"""This module provides a set of utilities for grouping peaks based on PAFs.

Part affinity fields (PAFs) are a representation used to resolve the peak grouping
problem for multi-instance pose estimation [1].

They are a convenient way to represent directed graphs with support in image space. For
each edge, a PAF can be represented by an image with two channels, corresponding to the
x and y components of a unit vector pointing along the direction of the underlying
directed graph formed by the connections of the landmarks belonging to an instance.

Given a pair of putatively connected landmarks, the agreement between the line segment
that connects them and the PAF vectors found at the coordinates along the same line can
be used as a measure of "connectedness". These scores can then be used to guide the
instance-wise grouping of landmarks.

This image space representation is particularly useful as it is amenable to neural
network-based prediction from unlabeled images.

References:
    .. [1] Zhe Cao, Tomas Simon, Shih-En Wei, Yaser Sheikh. Realtime Multi-Person 2D
       Pose Estimation using Part Affinity Fields. In _CVPR_, 2017.
"""

import attr
from typing import Dict, List, Union, Tuple, Text
import tensorflow as tf
import numpy as np
from scipy.optimize import linear_sum_assignment
from sleap.nn.config import MultiInstanceConfig


@attr.s(auto_attribs=True, slots=True, frozen=True)
class PeakID:
    node_ind: int
    peak_ind: int


@attr.s(auto_attribs=True, slots=True, frozen=True)
class EdgeType:
    src_node_ind: int
    dst_node_ind: int


@attr.s(auto_attribs=True, slots=True)
class EdgeConnection:
    src_peak_ind: int
    dst_peak_ind: int
    score: float


def assign_connections_to_instances(
    connections: Dict[EdgeType, List[EdgeConnection]],
    min_instance_peaks: Union[int, float] = 0,
    n_nodes: int = None,
) -> Dict[PeakID, int]:
    """Assigns connected edges to instances via greedy graph partitioning.

    Args:
        connections: A dict that maps EdgeType to a list of EdgeConnections found
            through connection scoring. This can be generated by the
            filter_connection_candidates function.
        min_instance_peaks: If this is greater than 0, grouped instances with fewer
            assigned peaks than this threshold will be excluded. If a float in the
            range (0., 1.] is provided, this is interpreted as a fraction of the total
            number of nodes in the skeleton. If an integer is provided, this is the
            absolute minimum number of peaks.
        n_nodes: Total node type count. Used to convert min_instance_peaks to an
            absolute number when a fraction is specified. If not provided, the node
            count is inferred from the unique node inds in connections.

    Returns:
        instance_assignments: A dict mapping PeakID to a unique instance ID specified
        as an integer.

        A PeakID is a tuple of (node_type_ind, peak_ind), where the peak_ind is the
        index or identifier specified in a EdgeConnection as a src_peak_ind or
        dst_peak_ind.

    Note:
        Instance IDs are not necessarily consecutive since some instances may be
        filtered out during the partitioning or filtering.

        This function expects connections from a single sample/frame!
    """

    # Grouping table that maps PeakID(node_ind, peak_ind) to an instance_id.
    instance_assignments = dict()

    # Loop through edge types.
    for edge_type, edge_connections in connections.items():

        # Loop through connections for the current edge.
        for connection in edge_connections:

            # Notation: specific peaks are identified by (node_ind, peak_ind).
            src_id = PeakID(edge_type.src_node_ind, connection.src_peak_ind)
            dst_id = PeakID(edge_type.dst_node_ind, connection.dst_peak_ind)

            # Get instance assignments for the connection peaks.
            src_instance = instance_assignments.get(src_id, None)
            dst_instance = instance_assignments.get(dst_id, None)

            if src_instance is None and dst_instance is None:
                # Case 1: Neither peak is assigned to an instance yet. We'll create a
                # new instance to hold both.
                new_instance = max(instance_assignments.values(), default=-1) + 1
                instance_assignments[src_id] = new_instance
                instance_assignments[dst_id] = new_instance

            elif src_instance is not None and dst_instance is None:
                # Case 2: The source peak is assigned already, but not the destination
                # peak. We'll assign the destination peak to the same instance as the
                # source.
                instance_assignments[dst_id] = src_instance

            elif src_instance is not None and dst_instance is not None:
                # Case 3: Both peaks have been assigned. We'll update the destination
                # peak to be a part of the source peak instance.
                instance_assignments[dst_id] = src_instance

                # We'll also check if they form disconnected subgraphs, in which case
                # we'll merge them by assigning all peaks belonging to the destination
                # peak's instance to the source peak's instance.
                src_instance_nodes = set(
                    peak_id.node_ind
                    for peak_id, instance in instance_assignments.items()
                    if instance == src_instance
                )
                dst_instance_nodes = set(
                    peak_id.node_ind
                    for peak_id, instance in instance_assignments.items()
                    if instance == dst_instance
                )

                if len(src_instance_nodes.intersection(dst_instance_nodes)) == 0:
                    for peak_id in instance_assignments:
                        if instance_assignments[peak_id] == dst_instance:
                            instance_assignments[peak_id] = src_instance

    if min_instance_peaks > 0:
        if isinstance(min_instance_peaks, float):

            if n_nodes is None:
                # Infer number of nodes if not specified.
                all_node_types = set()
                for edge_type in connections:
                    all_node_types.add(edge_type.src_node_ind)
                    all_node_types.add(edge_type.dst_node_ind)
                n_nodes = len(all_node_types)

            # Calculate minimum threshold.
            min_instance_peaks = int(min_instance_peaks * n_nodes)

        # Compute instance peak counts.
        instance_ids, instance_peak_counts = np.unique(
            list(instance_assignments.values()), return_counts=True
        )
        instance_peak_counts = {
            instance: peaks_count
            for instance, peaks_count in zip(instance_ids, instance_peak_counts)
        }

        # Filter out small instances.
        instance_assignments = {
            peak_id: instance
            for peak_id, instance in instance_assignments.items()
            if instance_peak_counts[instance] >= min_instance_peaks
        }

    return instance_assignments


def make_predicted_instances(peaks, peak_scores, connections, instance_assignments):
    # Ensure instance IDs are contiguous.
    instance_ids, instance_inds = np.unique(
        list(instance_assignments.values()), return_inverse=True
    )
    for peak_id, instance_ind in zip(instance_assignments.keys(), instance_inds):
        instance_assignments[peak_id] = instance_ind
    n_instances = len(instance_ids)

    # Compute instance scores as the sum of all edge scores.
    predicted_instance_scores = np.full((n_instances,), 0.0, dtype="float32")

    for edge_type, edge_connections in connections.items():
        # Loop over all connections for this edge type.
        for edge_connection in edge_connections:
            # Look up the source peak.
            src_peak_id = PeakID(
                node_ind=edge_type.src_node_ind, peak_ind=edge_connection.src_peak_ind
            )
            if src_peak_id in instance_assignments:
                # Add to the total instance score.
                instance_ind = instance_assignments[src_peak_id]
                predicted_instance_scores[instance_ind] += edge_connection.score

                # Sanity check: both peaks in the edge should have been assigned to the
                # same instance.
                dst_peak_id = PeakID(
                    node_ind=edge_type.dst_node_ind,
                    peak_ind=edge_connection.dst_peak_ind,
                )
                assert instance_ind == instance_assignments[dst_peak_id]

    # Fill in assigned peak data.
    # n_nodes = peaks.shape[0]
    n_nodes = len(peaks)
    predicted_instances = np.full((n_instances, n_nodes, 2), np.nan, dtype="float32")
    predicted_peak_scores = np.full((n_instances, n_nodes), np.nan, dtype="float32")
    for peak_id, instance_ind in instance_assignments.items():
        predicted_instances[instance_ind, peak_id.node_ind, :] = peaks[
            peak_id.node_ind
        ][peak_id.peak_ind]
        predicted_peak_scores[instance_ind, peak_id.node_ind] = peak_scores[
            peak_id.node_ind
        ][peak_id.peak_ind]

    return predicted_instances, predicted_peak_scores, predicted_instance_scores


@attr.s(auto_attribs=True)
class PAFScorer:
    part_names: List[Text]
    edges: List[Tuple[Text, Text]]
    pafs_stride: int
    max_edge_length: float = 128
    min_edge_score: float = 0.05
    n_points: int = 10
    min_instance_peaks: Union[int, float] = 0

    edge_inds: List[Tuple[int, int]] = attr.ib(init=False)
    edge_types: List[EdgeType] = attr.ib(init=False)
    n_nodes: int = attr.ib(init=False)
    n_edges: int = attr.ib(init=False)

    def __attrs_post_init__(self):
        self.edge_inds = [
            (self.part_names.index(src), self.part_names.index(dst))
            for (src, dst) in self.edges
        ]
        self.edge_types = [
            EdgeType(src_node, dst_node) for src_node, dst_node in self.edge_inds
        ]

        self.n_nodes = len(self.part_names)
        self.n_edges = len(self.edges)

    @classmethod
    def from_config(
        cls,
        config: MultiInstanceConfig,
        max_edge_length: float = 128,
        min_edge_score: float = 0.05,
        n_points: int = 10,
        min_instance_peaks: Union[int, float] = 0,
    ) -> "PAFScorer":
        return cls(
            part_names=config.confmaps.part_names,
            edges=config.pafs.edges,
            pafs_stride=config.pafs.output_stride,
            max_edge_length=max_edge_length,
            min_edge_score=min_edge_score,
            n_points=n_points,
            min_instance_peaks=min_instance_peaks,
        )

    def sample_edge_line(self, paf, src_peak, dst_peak):

        paf_x = tf.gather(paf, 0, axis=-1)
        paf_y = tf.gather(paf, 1, axis=-1)

        max_x = tf.cast(tf.shape(paf_x)[1] - 1, tf.float32)
        max_y = tf.cast(tf.shape(paf_x)[0] - 1, tf.float32)

        line_x = tf.linspace(src_peak[0], dst_peak[0], self.n_points)
        line_y = tf.linspace(src_peak[1], dst_peak[1], self.n_points)

        line_x /= tf.cast(self.pafs_stride, tf.float32)
        line_y /= tf.cast(self.pafs_stride, tf.float32)

        line_x = tf.clip_by_value(tf.round(line_x), 0, max_x)
        line_y = tf.clip_by_value(tf.round(line_y), 0, max_y)

        line_x = tf.cast(line_x, tf.int32)
        line_y = tf.cast(line_y, tf.int32)

        line_subs = tf.stack([line_y, line_x], axis=1)

        line_paf_x = tf.gather_nd(paf_x, line_subs)
        line_paf_y = tf.gather_nd(paf_y, line_subs)

        line_paf = tf.stack([line_paf_x, line_paf_y], axis=-1)  # (n_points, 2)

        return line_paf

    def score_pair(self, line_paf, src_peak, dst_peak):

        # Normalized spatial vector
        spatial_vec = dst_peak - src_peak
        spatial_vec_length = tf.norm(spatial_vec)
        spatial_vec /= spatial_vec_length

        # Compute dot product scores
        line_scores = tf.squeeze(
            line_paf @ tf.expand_dims(spatial_vec, axis=-1), axis=-1
        )  # (n_points,)

        # Compute average line scores with distance penalty.
        dist_penalty = (
            tf.cast(self.max_edge_length, tf.float32) / spatial_vec_length
        ) - 1

        # Compute overall line score
        line_score = tf.reduce_mean(line_scores)
        line_score_with_dist_penalty = line_score + tf.minimum(dist_penalty, 0)

        # Compute fraction of connections above threshold.
        fraction_correct = tf.reduce_mean(
            tf.cast(line_scores > self.min_edge_score, tf.float32)
        )

        return line_score_with_dist_penalty, fraction_correct

    def score_edge(self, paf, src_peaks, dst_peaks):

        line_scores = tf.TensorArray(dtype=tf.float32, size=0, dynamic_size=True)
        fraction_correct = tf.TensorArray(dtype=tf.float32, size=0, dynamic_size=True)

        # Iterate over source peaks.
        for i in range(len(src_peaks)):

            line_scores_i = tf.TensorArray(dtype=tf.float32, size=0, dynamic_size=True)
            fraction_correct_i = tf.TensorArray(
                dtype=tf.float32, size=0, dynamic_size=True
            )

            # Iterate over destination peaks.
            for j in range(len(dst_peaks)):

                # Pull out peaks.
                src_peak = src_peaks[i]
                dst_peak = dst_peaks[j]

                # Get line integral from PAF tensor.
                line_paf = self.sample_edge_line(paf, src_peak, dst_peak)

                # Compute scores from line integral.
                line_score_ij, fraction_correct_ij = self.score_pair(
                    line_paf, src_peak, dst_peak
                )

                line_scores_i = line_scores_i.write(j, line_score_ij)
                fraction_correct_i = fraction_correct_i.write(j, fraction_correct_ij)

            line_scores_i = line_scores_i.stack()
            fraction_correct_i = fraction_correct_i.stack()
            line_scores = line_scores.write(i, line_scores_i)
            fraction_correct = line_scores.write(i, fraction_correct_i)

        line_scores = line_scores.stack()
        fraction_correct = fraction_correct.stack()

        return line_scores, fraction_correct

    def score_and_match_edge(self, paf, src_peaks, dst_peaks):
        # Compute scores from PAF line integrals.
        line_scores, fraction_correct = self.score_edge(paf, src_peaks, dst_peaks)

        # Match edge candidates.
        src_inds, dst_inds = tf.py_function(
            linear_sum_assignment, inp=[-line_scores], Tout=[tf.int32, tf.int32]
        )

        # Pull out matched scores.
        match_subs = tf.stack([src_inds, dst_inds], axis=1)
        line_scores = tf.gather_nd(line_scores, match_subs)
        fraction_correct = tf.gather_nd(fraction_correct, match_subs)

        return src_inds, dst_inds, line_scores, fraction_correct

    def match_all_peaks(self, pafs, flat_peaks, flat_channel_inds):

        # Make sure PAFs are unflattened into (..., n_edges, 2).
        pafs = tf.reshape(pafs, [tf.shape(pafs)[0], tf.shape(pafs)[1], -1, 2])

        # Sort peaks by channel
        sort_idx = tf.argsort(flat_channel_inds)
        peaks = tf.gather(flat_peaks, sort_idx)
        channel_inds = tf.gather(flat_channel_inds, sort_idx)

        # Group peaks by channel
        peaks = tf.RaggedTensor.from_value_rowids(
            values=peaks, value_rowids=channel_inds, nrows=self.n_nodes
        )

        # Initialize dynamically sized containers.
        all_edge_inds = tf.TensorArray(dtype=tf.int32, size=0, dynamic_size=True)
        all_src_inds = tf.TensorArray(dtype=tf.int32, size=0, dynamic_size=True)
        all_dst_inds = tf.TensorArray(dtype=tf.int32, size=0, dynamic_size=True)
        all_line_scores = tf.TensorArray(dtype=tf.float32, size=0, dynamic_size=True)
        all_fraction_correct = tf.TensorArray(
            dtype=tf.float32, size=0, dynamic_size=True
        )

        # Iterate over edges.
        for edge_ind in range(self.n_edges):

            # Pull out edge data.
            paf = tf.gather(pafs, edge_ind, axis=-2)
            src_peaks = peaks[self.edge_inds[edge_ind][0]]
            dst_peaks = peaks[self.edge_inds[edge_ind][1]]

            # Score the edge.
            (
                src_inds,
                dst_inds,
                line_scores,
                fraction_correct,
            ) = self.score_and_match_edge(paf, src_peaks, dst_peaks)

            # Store edge results.
            all_edge_inds = all_edge_inds.write(
                edge_ind, tf.broadcast_to(edge_ind, tf.shape(src_inds))
            )
            all_src_inds = all_src_inds.write(edge_ind, src_inds)
            all_dst_inds = all_dst_inds.write(edge_ind, dst_inds)
            all_line_scores = all_line_scores.write(edge_ind, line_scores)
            all_fraction_correct = all_fraction_correct.write(
                edge_ind, fraction_correct
            )

        # Concatenate dynamic tensors into flat ones. These can be split again by using
        # flat_edge_inds as a grouping vector.
        flat_edge_inds = all_edge_inds.concat()
        flat_src_inds = all_src_inds.concat()
        flat_dst_inds = all_dst_inds.concat()
        flat_line_scores = all_line_scores.concat()
        flat_fraction_correct = all_fraction_correct.concat()

        return (
            flat_edge_inds,
            flat_src_inds,
            flat_dst_inds,
            flat_line_scores,
            flat_fraction_correct,
        )

    def match_instances(
        self,
        flat_peaks,
        flat_peak_scores,
        flat_channel_inds,
        flat_edge_inds,
        flat_src_peak_inds,
        flat_dst_peak_inds,
        flat_line_scores,
        flat_fraction_correct,
    ):

        # Convert all the data to numpy arrays.
        flat_peaks = flat_peaks.numpy()
        flat_peak_scores = flat_peak_scores.numpy()
        flat_channel_inds = flat_channel_inds.numpy()
        flat_edge_inds = flat_edge_inds.numpy()
        flat_src_peak_inds = flat_src_peak_inds.numpy()
        flat_dst_peak_inds = flat_dst_peak_inds.numpy()
        flat_line_scores = flat_line_scores.numpy()
        flat_fraction_correct = flat_fraction_correct.numpy()

        # Group peaks by channel.
        peaks = []
        peak_scores = []
        for i in range(self.n_nodes):
            in_channel = flat_channel_inds == i
            peaks.append(flat_peaks[in_channel])
            peak_scores.append(flat_peak_scores[in_channel])

        # Group connection data by edge.
        src_peak_inds = []
        dst_peak_inds = []
        line_scores = []
        fraction_correct = []
        for i in range(self.n_edges):
            in_edge = flat_edge_inds == i
            src_peak_inds.append(flat_src_peak_inds[in_edge])
            dst_peak_inds.append(flat_dst_peak_inds[in_edge])
            line_scores.append(flat_line_scores[in_edge])
            fraction_correct.append(flat_fraction_correct[in_edge])

        # Form connections structure.
        connections = dict()
        for edge_ind, (src_peak_ind, dst_peak_ind, line_score) in enumerate(
            zip(src_peak_inds, dst_peak_inds, line_scores)
        ):
            connections[self.edge_types[edge_ind]] = [
                EdgeConnection(src, dst, score)
                for src, dst, score in zip(src_peak_ind, dst_peak_ind, line_score)
            ]

        # Bipartite graph partitioning to group connections into instances.
        instance_assignments = assign_connections_to_instances(
            connections,
            min_instance_peaks=self.min_instance_peaks,
            n_nodes=self.n_nodes,
        )

        # Gather the data by instance.
        (
            predicted_instances,
            predicted_peak_scores,
            predicted_instance_scores,
        ) = make_predicted_instances(
            peaks, peak_scores, connections, instance_assignments
        )

        return predicted_instances, predicted_peak_scores, predicted_instance_scores

    def match_with_pafs(self, pafs, flat_peaks, flat_peak_scores, flat_channel_inds):
        # Match peaks within each edge using PAF scores.
        (
            flat_edge_inds,
            flat_src_peak_inds,
            flat_dst_peak_inds,
            flat_line_scores,
            flat_fraction_correct,
        ) = self.match_all_peaks(pafs, flat_peaks, flat_channel_inds)

        # Given matched peaks, group them into instances.
        (
            predicted_instances,
            predicted_peak_scores,
            predicted_instance_scores,
        ) = tf.py_function(
            self.match_instances,
            inp=[
                flat_peaks,
                flat_peak_scores,
                flat_channel_inds,
                flat_edge_inds,
                flat_src_peak_inds,
                flat_dst_peak_inds,
                flat_line_scores,
                flat_fraction_correct,
            ],
            Tout=[tf.float32, tf.float32, tf.float32],
        )

        return predicted_instances, predicted_peak_scores, predicted_instance_scores


@attr.s(auto_attribs=True)
class PartAffinityFieldInstanceGrouper:
    paf_scorer: PAFScorer

    peaks_key: Text = "predicted_peaks"
    peak_scores_key: Text = "predicted_peak_confidences"
    channel_inds_key: Text = "predicted_peak_channel_inds"
    pafs_key: Text = "predicted_part_affinity_fields"

    predicted_instances_key: Text = "predicted_instances"
    predicted_peak_scores_key: Text = "predicted_peak_scores"
    predicted_instance_scores_key: Text = "predicted_instance_scores"

    keep_pafs: bool = False

    @classmethod
    def from_config(
        cls,
        config: MultiInstanceConfig,
        max_edge_length: float = 128,
        min_edge_score: float = 0.05,
        n_points: int = 10,
        min_instance_peaks: Union[int, float] = 0,
        peaks_key: Text = "predicted_peaks",
        peak_scores_key: Text = "predicted_peak_confidences",
        channel_inds_key: Text = "predicted_peak_channel_inds",
        pafs_key: Text = "predicted_part_affinity_fields",
        predicted_instances_key: Text = "predicted_instances",
        predicted_peak_scores_key: Text = "predicted_peak_scores",
        predicted_instance_scores_key: Text = "predicted_instance_scores",
        keep_pafs: bool = False
    ) -> "PartAffinityFieldInstanceGrouper":
        return cls(
            paf_scorer=PAFScorer.from_config(
                config,
                max_edge_length=max_edge_length,
                min_edge_score=min_edge_score,
                n_points=n_points,
                min_instance_peaks=min_instance_peaks,
            ),
            peaks_key=peaks_key,
            peak_scores_key=peak_scores_key,
            channel_inds_key=channel_inds_key,
            pafs_key=pafs_key,
            predicted_instances_key=predicted_instances_key,
            predicted_peak_scores_key=predicted_peak_scores_key,
            predicted_instance_scores_key=predicted_instance_scores_key,
            keep_pafs=keep_pafs
        )

    @property
    def input_keys(self) -> List[Text]:
        return [
            self.peaks_key,
            self.peak_scores_key,
            self.channel_inds_key,
            self.pafs_key,
        ]

    @property
    def output_keys(self) -> List[Text]:
        return self.input_keys + [
            self.predicted_instances_key,
            self.predicted_peak_scores_key,
            self.predicted_instance_scores_key,
        ]

    def transform_dataset(self, input_ds: tf.data.Dataset) -> tf.data.Dataset:

        def group_instances(example):
            # Pull out example data.
            pafs = example[self.pafs_key]
            flat_peaks = example[self.peaks_key]
            flat_peak_scores = example[self.peak_scores_key]
            flat_channel_inds = example[self.channel_inds_key]

            # Run matching.
            (
                predicted_instances,
                predicted_peak_scores,
                predicted_instance_scores,
            ) = self.paf_scorer.match_with_pafs(
                pafs, flat_peaks, flat_peak_scores, flat_channel_inds
            )

            # Update example.
            example[self.predicted_instances_key] = predicted_instances
            example[self.predicted_peak_scores_key] = predicted_peak_scores
            example[self.predicted_instance_scores_key] = predicted_instance_scores

            if not self.keep_pafs:
                # Drop PAFs.
                example.pop(self.pafs_key)

            return example

        output_ds = input_ds.map(
            group_instances, num_parallel_calls=tf.data.experimental.AUTOTUNE
        )
        return output_ds
