# This file is part of pipe_base.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (http://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Module defining GraphBuilder class and related methods.
"""

__all__ = ['GraphBuilder']

# -------------------------------
#  Imports of standard modules --
# -------------------------------
import copy
from collections import namedtuple
from itertools import chain
import logging

# -----------------------------
#  Imports for other modules --
# -----------------------------
from .graph import QuantumGraphTaskNodes, QuantumGraph
from lsst.daf.butler import Quantum, DatasetRef, DimensionSet

# ----------------------------------
#  Local non-exported definitions --
# ----------------------------------

_LOG = logging.getLogger(__name__.partition(".")[2])

# Tuple containing TaskDef, its input dataset types and output dataset types
#
# Attributes
# ----------
# taskDef : `TaskDef`
# inputs : `set` of `DatasetType`
# outputs : `set` of `DatasetType`
# initTnputs : `set` of `DatasetType`
# initOutputs : `set` of `DatasetType`
# perDatasetTypeDimensions : `~lsst.daf.butler.DimensionSet`
# prerequisite : `set` of `DatasetType`
_TaskDatasetTypes = namedtuple("_TaskDatasetTypes", ("taskDef", "inputs", "outputs",
                                                     "initInputs", "initOutputs",
                                                     "perDatasetTypeDimensions", "prerequisite"))


class GraphBuilderError(Exception):
    """Base class for exceptions generated by graph builder.
    """
    pass


class OutputExistsError(GraphBuilderError):
    """Exception generated when output datasets already exist.
    """

    def __init__(self, taskName, refs):
        refs = ', '.join(str(ref) for ref in refs)
        msg = "Output datasets already exist for task {}: {}".format(taskName, refs)
        GraphBuilderError.__init__(self, msg)


class PrerequisiteMissingError(GraphBuilderError):
    """Exception generated when a prerequisite dataset does not exist.
    """
    pass


class GraphBuilder(object):
    """
    GraphBuilder class is responsible for building task execution graph from
    a Pipeline.

    Parameters
    ----------
    taskFactory : `TaskFactory`
        Factory object used to load/instantiate PipelineTasks
    registry : `~lsst.daf.butler.Registry`
        Data butler instance.
    skipExisting : `bool`, optional
        If ``True`` (default) then Quantum is not created if all its outputs
        already exist, otherwise exception is raised.
    """

    def __init__(self, taskFactory, registry, skipExisting=True):
        self.taskFactory = taskFactory
        self.registry = registry
        self.dimensions = registry.dimensions
        self.skipExisting = skipExisting

    def _loadTaskClass(self, taskDef):
        """Make sure task class is loaded.

        Load task class, update task name to make sure it is fully-qualified,
        do not update original taskDef in a Pipeline though.

        Parameters
        ----------
        taskDef : `TaskDef`

        Returns
        -------
        `TaskDef` instance, may be the same as parameter if task class is
        already loaded.
        """
        if taskDef.taskClass is None:
            tClass, tName = self.taskFactory.loadTaskClass(taskDef.taskName)
            taskDef = copy.copy(taskDef)
            taskDef.taskClass = tClass
            taskDef.taskName = tName
        return taskDef

    def makeGraph(self, pipeline, originInfo, userQuery):
        """Create execution graph for a pipeline.

        Parameters
        ----------
        pipeline : `Pipeline`
            Pipeline definition, task names/classes and their configs.
        originInfo : `~lsst.daf.butler.DatasetOriginInfo`
            Object which provides names of the input/output collections.
        userQuery : `str`
            String which defunes user-defined selection for registry, should be
            empty or `None` if there is no restrictions on data selection.

        Returns
        -------
        graph : `QuantumGraph`

        Raises
        ------
        UserExpressionError
            Raised when user expression cannot be parsed.
        OutputExistsError
            Raised when output datasets already exist.
        Exception
            Other exceptions types may be raised by underlying registry
            classes.
        """

        # make sure all task classes are loaded
        taskList = [self._loadTaskClass(taskDef) for taskDef in pipeline]

        # collect inputs/outputs from each task
        taskDatasets = []
        for taskDef in taskList:
            taskClass = taskDef.taskClass
            inputs = {k: v.datasetType for k, v in taskClass.getInputDatasetTypes(taskDef.config).items()}
            prerequisite = set(inputs[k] for k in taskClass.getPrerequisiteDatasetTypes(taskDef.config))
            taskIo = [inputs.values()]
            for attr in ("Output", "InitInput", "InitOutput"):
                getter = getattr(taskClass, f"get{attr}DatasetTypes")
                ioObject = getter(taskDef.config) or {}
                taskIo.append(set(dsTypeDescr.datasetType for dsTypeDescr in ioObject.values()))
            perDatasetTypeDimensions = DimensionSet(self.registry.dimensions,
                                                    taskClass.getPerDatasetTypeDimensions(taskDef.config))
            taskDatasets.append(_TaskDatasetTypes(taskDef, *taskIo, prerequisite=prerequisite,
                                                  perDatasetTypeDimensions=perDatasetTypeDimensions))

        perDatasetTypeDimensions = self._extractPerDatasetTypeDimensions(taskDatasets)

        # categorize dataset types for the full Pipeline
        required, optional, prerequisite, initInputs, initOutputs = self._makeFullIODatasetTypes(taskDatasets)

        # make a graph
        return self._makeGraph(taskDatasets, required, optional, prerequisite, initInputs, initOutputs,
                               originInfo, userQuery, perDatasetTypeDimensions=perDatasetTypeDimensions)

    def _extractPerDatasetTypeDimensions(self, taskDatasets):
        """Return the complete set of all per-DatasetType dimensions declared
        by any task.

        Per-DatasetType dimensions are those that need not have the same values
        for different Datasets within a Quantum.

        Parameters
        ----------
        taskDatasets : sequence of `_TaskDatasetTypes`
            Information for each task in the pipeline.

        Returns
        -------
        perDatasetTypeDimensions : `~lsst.daf.butler.DimensionSet`
            All per-DatasetType dimensions.

        Raises
        ------
        ValueError
            Raised if tasks disagree on whether a dimension is declared
            per-DatasetType.
        """
        # Empty dimension set, just used to construct more DimensionSets via
        # union method.
        noDimensions = DimensionSet(self.registry.dimensions, ())
        # Construct pipeline-wide perDatasetTypeDimensions set from union of
        # all Task-level perDatasetTypeDimensions.
        perDatasetTypeDimensions = noDimensions.union(
            *[taskDs.perDatasetTypeDimensions for taskDs in taskDatasets]
        )
        # Check that no tasks want any of these as common (i.e. not
        # per-DatasetType) dimensions.
        for taskDs in taskDatasets:
            allTaskDimensions = noDimensions.union(
                *[datasetType.dimensions for datasetType in chain(taskDs.inputs, taskDs.outputs)]
            )
            commonTaskDimensions = allTaskDimensions - taskDs.perDatasetTypeDimensions
            if not commonTaskDimensions.isdisjoint(perDatasetTypeDimensions):
                overlap = commonTaskDimensions.intersections(perDatasetTypeDimensions)
                raise ValueError(
                    f"Task {taskDs.taskDef.taskName} uses dimensions {overlap} without declaring them "
                    f"per-DatasetType, but they are declared per-DatasetType by another task."
                )
        return perDatasetTypeDimensions

    def _makeFullIODatasetTypes(self, taskDatasets):
        """Returns full set of input and output dataset types for all tasks.

        Parameters
        ----------
        taskDatasets : sequence of `_TaskDatasetTypes`
            Tasks with their inputs, outputs, initInputs and initOutputs.

        Returns
        -------
        required : `set` of `~lsst.daf.butler.DatasetType`
            Datasets that must exist in the repository in order to generate
            a QuantumGraph node that consumes them.
        optional : `set` of `~lsst.daf.butler.DatasetType`
            Datasets that will be produced by the graph, but may exist in the
            repository.  If ``self.skipExisting`` is `True` and all outputs of
            a particular node already exist, it will be skipped.  Otherwise
            pre-existing datasets of these types will cause
            `OutputExistsError` to be raised.
        prerequisite : `set` of `~lsst.daf.butler.DatasetType`
            Datasets that must exist in the repository, but whose absence
            should cause `PrerequisiteMissingError` to be raised if they
            are needed by any graph node that would otherwise be created.
        initInputs : `set` of `~lsst.daf.butler.DatasetType`
            Datasets used as init method inputs by the pipeline.
        initOutputs : `set` of `~lsst.daf.butler.DatasetType`
            Datasets used as init method outputs by the pipeline.
        """
        # to build initial dataset graph we have to collect info about all
        # datasets to be used by this pipeline
        allDatasetTypes = {}
        required = set()
        optional = set()
        prerequisite = set()
        initInputs = set()
        initOutputs = set()
        for taskDs in taskDatasets:
            for ioType, ioSet in zip(("inputs", "outputs", "prerequisite", "initInputs", "initOutputs"),
                                     (required, optional, prerequisite, initInputs, initOutputs)):
                for dsType in getattr(taskDs, ioType):
                    ioSet.add(dsType.name)
                    allDatasetTypes[dsType.name] = dsType

        # Any dataset the pipeline produces can't be required or prerequisite
        required -= optional
        prerequisite -= optional

        # remove initOutputs from initInputs
        initInputs -= initOutputs

        required = set(allDatasetTypes[name] for name in required)
        optional = set(allDatasetTypes[name] for name in optional)
        prerequisite = set(allDatasetTypes[name] for name in prerequisite)
        initInputs = set(allDatasetTypes[name] for name in initInputs)
        initOutputs = set(allDatasetTypes[name] for name in initOutputs)
        return required, optional, prerequisite, initInputs, initOutputs

    def _makeGraph(self, taskDatasets, required, optional, prerequisite,
                   initInputs, initOutputs, originInfo, userQuery,
                   perDatasetTypeDimensions=()):
        """Make QuantumGraph instance.

        Parameters
        ----------
        taskDatasets : sequence of `_TaskDatasetTypes`
            Tasks with their inputs and outputs.
        required : `set` of `~lsst.daf.butler.DatasetType`
            Datasets that must exist in the repository in order to generate
            a QuantumGraph node that consumes them.
        optional : `set` of `~lsst.daf.butler.DatasetType`
            Datasets that will be produced by the graph, but may exist in
            the repository.  If ``self.skipExisting`` and all outputs of a
            particular node already exist, it will be skipped.  Otherwise
            pre-existing datasets of these types will cause
            `OutputExistsError` to be raised.
        prerequisite : `set` of `~lsst.daf.butler.DatasetType`
            Datasets that must exist in the repository, but whose absence
            should cause `PrerequisiteMissingError` to be raised if they
            are needed by any graph node that would otherwise be created.
        initInputs : `set` of `DatasetType`
            Datasets which should exist in input repository, and will be used
            in task initialization
        initOutputs : `set` of `DatasetType`
            Datasets which which will be created in task initialization
        originInfo : `DatasetOriginInfo`
            Object which provides names of the input/output collections.
        userQuery : `str`
            String which defines user-defined selection for registry, should be
            empty or `None` if there is no restrictions on data selection.
        perDatasetTypeDimensions : iterable of `Dimension` or `str`
            Dimensions (or names thereof) that may have different values for
            different dataset types within the same quantum.

        Returns
        -------
        `QuantumGraph` instance.
        """
        rows = self.registry.selectMultipleDatasetTypes(
            originInfo, userQuery,
            required=required, optional=optional, prerequisite=prerequisite,
            perDatasetTypeDimensions=perDatasetTypeDimensions
        )

        # store result locally for multi-pass algorithm below
        # TODO: change it to single pass
        dimensionVerse = []
        try:
            for row in rows:
                _LOG.debug("row: %s", row)
                dimensionVerse.append(row)
        except LookupError as err:
            raise PrerequisiteMissingError(str(err)) from err

        # Next step is to group by task quantum dimensions
        qgraph = QuantumGraph()
        qgraph._inputDatasetTypes = (required | prerequisite)
        qgraph._outputDatasetTypes = optional
        for dsType in initInputs:
            for collection in originInfo.getInputCollections(dsType.name):
                result = self.registry.find(collection, dsType)
                if result is not None:
                    qgraph.initInputs.append(result)
                    break
            else:
                raise GraphBuilderError(f"Could not find initInput {dsType.name} in any input"
                                        " collection")
        for dsType in initOutputs:
            qgraph.initOutputs.append(DatasetRef(dsType, {}))

        for taskDss in taskDatasets:
            taskQuantaInputs = {}    # key is the quantum dataId (as tuple)
            taskQuantaOutputs = {}   # key is the quantum dataId (as tuple)
            qlinks = []
            for dimensionName in taskDss.taskDef.config.quantum.dimensions:
                dimension = self.dimensions[dimensionName]
                qlinks += dimension.links()
            _LOG.debug("task %s qdimensions: %s", taskDss.taskDef.label, qlinks)

            # some rows will be non-unique for subset of dimensions, create
            # temporary structure to remove duplicates
            for row in dimensionVerse:
                qkey = tuple((col, row.dataId[col]) for col in qlinks)
                _LOG.debug("qkey: %s", qkey)

                def _datasetRefKey(datasetRef):
                    return tuple(sorted(datasetRef.dataId.items()))

                qinputs = taskQuantaInputs.setdefault(qkey, {})
                for dsType in taskDss.inputs:
                    datasetRefs = qinputs.setdefault(dsType, {})
                    datasetRef = row.datasetRefs[dsType]
                    datasetRefs[_datasetRefKey(datasetRef)] = datasetRef
                    _LOG.debug("add input datasetRef: %s %s", dsType.name, datasetRef)

                qoutputs = taskQuantaOutputs.setdefault(qkey, {})
                for dsType in taskDss.outputs:
                    datasetRefs = qoutputs.setdefault(dsType, {})
                    datasetRef = row.datasetRefs[dsType]
                    datasetRefs[_datasetRefKey(datasetRef)] = datasetRef
                    _LOG.debug("add output datasetRef: %s %s", dsType.name, datasetRef)

            # all nodes for this task
            quanta = []
            for qkey in taskQuantaInputs:
                # taskQuantaInputs and taskQuantaOutputs have the same keys
                _LOG.debug("make quantum for qkey: %s", qkey)
                quantum = Quantum(run=None, task=None)

                # add all outputs, but check first that outputs don't exist
                outputs = list(chain.from_iterable(datasetRefs.values()
                                                   for datasetRefs in taskQuantaOutputs[qkey].values()))
                for ref in outputs:
                    _LOG.debug("add output: %s", ref)
                if self.skipExisting and all(ref.id is not None for ref in outputs):
                    _LOG.debug("all output datasetRefs already exist, skip quantum")
                    continue
                if any(ref.id is not None for ref in outputs):
                    # some outputs exist, can't override them
                    raise OutputExistsError(taskDss.taskDef.taskName, outputs)

                for ref in outputs:
                    quantum.addOutput(ref)

                # add all inputs
                for datasetRefs in taskQuantaInputs[qkey].values():
                    for ref in datasetRefs.values():
                        quantum.addPredictedInput(ref)
                        _LOG.debug("add input: %s", ref)

                quanta.append(quantum)

            qgraph.append(QuantumGraphTaskNodes(taskDss.taskDef, quanta))

        return qgraph
