# Copyright 2020 University of Groningen
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from ..selectors import is_water, selector_has_position
from .processor import Processor
from ..molecule import Molecule

import collections
import itertools
import networkx as nx
import numpy as np


def expand_to_list(num_or_list, length):
    try:
        len(num_or_list)
    except TypeError:
        num_or_list = [num_or_list] * length
    return num_or_list


def constrained_kmeans(data, num_clusters,
                       clust_sizes=4, tolerances=0, init_clusters='fixed',
                       precision=1e5, max_iter=100):
    """
    Clusters data in `num_clusters` clusters, where all clusters will contain
    `clust_sizes` \u00b1 `tolerances` items. If this is not possible, an exception
    is raised. If `tolerances` is not 0 clusters can "borrow" or "donate" up to
    `tolerances` items to a virtual data point that is at a distance of 10 units
    in order to fulfill the constraints.

    Initial cluster placement is controlled by `init_clusters`. If this is
    "fixed" initial clusters will be placed at the origin. If "random", random
    data points will be taken as initial clusters. Otherwise, `init_clusters`
    is taken as initial cluster positions.

    The underlying algorithm (:func:`~networkx.algorithms.flow.network_simplex`)
    requires all arguments to be integers, including the distances. Therefore
    all distances will be multiplied by `precision` before being rounded.

    Loosely based on [1]_.

    Parameters
    ----------
    data: numpy.ndarray
        The data points to cluster.
    num_clusters: int
        The number of clusters to create.
    clust_sizes: int or collections.abc.Sequence[int]
        The required number of items for a cluster, possibly per cluster.
    tolerances: int or collections.abc.Sequence[int]
        The allowed deviation of the number of items for a cluster, possibly per
        cluster.
    init_clusters: numpy.ndarray or str
        The initial cluster positions. If "fixed", all clusters will start at
        the origin. If "random", random data points will be taken as initial
        cluster positions. Otherwise, it should be an array containing the
        initial cluster positions.
    precision: float
        The precision with which to round distances to integers.
    max_iter: int
        The maximum number of iterations.

    Raises
    ------
    networkx.NetworkXUnfeasible
        If there is no way to fulfill the constraints.

    Notes
    -----
    Periodic boundary conditions are not taken into account in distance
    calculations.

    References
    ----------
    .. [1] P. Bradley, K. Bennett, A. Demiriz, Constrained k-means clustering, Microsoft Res. Redmond. 20 (2000) 9.

    Returns
    -------
    float
        The cost, corrected for `precision`.
    numpy.ndarray
        A numpy array of shape (num_clusters, data.shape[-1]) containing the
        cluster positions.
    numpy.ndarray
        A numpy array of shape (data.shape[0], num_clusters) containing 0 and 1,
        with 1 meaning a datapoint contributes to a cluster.
    """
    if data.ndim == 1:
        # np.atleast_2d adds the new dimension in the wrong place.
        data = data[:, np.newaxis]
    clust_sizes = expand_to_list(clust_sizes, num_clusters)
    tolerances = expand_to_list(tolerances, num_clusters)
    if len(clust_sizes) != num_clusters:
        raise IndexError('len(max_clust_sizes) must be num_clusters ({}), but '
                         'is {}'.format(num_clusters, len(clust_sizes)))
    if len(tolerances) != num_clusters:
        raise IndexError('len(tolerances) must be num_clusters ({}), but '
                         'is {}'.format(num_clusters, len(tolerances)))
    if isinstance(init_clusters, str) and init_clusters == 'fixed':
        clusters = np.zeros((num_clusters, data.shape[-1]))
    elif isinstance(init_clusters, str) and init_clusters == 'random':
        rng = np.random.default_rng()
        clusters = rng.choice(data, num_clusters, replace=False, axis=0)
    else:
        clusters = np.broadcast_to(init_clusters, (num_clusters, data.shape[-1]))

    # Time to build the DiGraph on which the minimal flow problem is solved.
    # Note that supply/demand are inverted relative to the paper.
    # The graph basically looks like:
    # Data -> clusters <=> artificial sink
    flow_graph = nx.DiGraph()
    flow_graph.add_nodes_from(range(len(data) + num_clusters + 1))
    data_nodes = list(range(len(data)))
    clust_nodes = list(range(len(data), len(data) + num_clusters))
    artificial_node = len(data) + num_clusters
    # Demand for artificial node
    rest_cost = len(data) - sum(clust_sizes)
    flow_graph.nodes[artificial_node]['demand'] = rest_cost
    for clust_n_idx in clust_nodes:
        # Edges between clusters and artificial node
        # The paper only has an edge from clusters to artificial node with an
        # infinite capacity and weight 0, since it only deals with a *minimal*
        # number of items per cluster (i.e. you can add more items to any
        # cluster to satisfy the cluster's requirements, and shove any
        # superfluous items on to the artificial node).
        # The capacity means the cluster can donate (or receive) a limited
        # number of supply from the artificial node, and this number exactly is
        # the deviation from the desired number of items per cluster.
        # The weight affects how often/easy it is to deviate from the required
        # number of items per cluster, even when a perfect/equal partitioning is
        # possible.
        flow_graph.add_edge(clust_n_idx, artificial_node,
                            weight=round(precision*100),
                            capacity=tolerances[clust_n_idx-len(data_nodes)])
        flow_graph.add_edge(artificial_node, clust_n_idx,
                            weight=round(precision*100),
                            capacity=tolerances[clust_n_idx-len(data_nodes)])
        # Demand for cluster nodes
        flow_graph.nodes[clust_n_idx]['demand'] = clust_sizes[clust_n_idx - len(data)]
    for data_n_idx in data_nodes:
        # Data supply
        flow_graph.nodes[data_n_idx]['demand'] = -1
    for data_n_idx, clust_n_idx in itertools.product(data_nodes, clust_nodes):
        # Edges between data and clusters
        # Capacity is meaningless, since data nodes have only 1 supply to give,
        # but it seems to help the network simplex a little.
        flow_graph.add_edge(data_n_idx, clust_n_idx, capacity=1)

    # Start clustering iterations.
    iter = 0
    flow_dict = {}
    while iter < max_iter:
        # Step 1: set up edge weights based on clustroid positions
        # TODO: PBC
        dists = np.sum((data[:, np.newaxis] - clusters[np.newaxis, :])**2, axis=-1)
        # When clusters are empty their clustroids become nan, which we can't
        # round to an int.
        dists[np.isnan(dists)] = 100
        for (data_idx, clust_idx), dist in np.ndenumerate(dists):
            # Network simplex (and capacity scaling) does not work with float
            # weights according to nx documentation. So round the distances to
            # an appropriate int. Magic.
            flow_graph.edges[data_idx, clust_idx + len(data)]['weight'] = round(precision*dist)

        prev_weights = flow_dict
        cost, flow_dict = nx.network_simplex(flow_graph)
        # cost, flow_dict = nx.capacity_scaling(flow_graph)

        if prev_weights == flow_dict:
            break
        # Step 2: make memberships and calculate new clustroid positions
        memberships = np.zeros((len(data), num_clusters))
        for data_idx in data_nodes:
            flow = flow_dict[data_idx]
            memberships[data_idx] = [flow[clust_n_idx] for clust_n_idx in clust_nodes]
        # TODO: PBC

        weights = memberships / memberships.sum(axis=0, keepdims=True)
        clusters = weights.T.dot(data)

        iter += 1

    return cost/precision, clusters, memberships, iter


def group_molecules(system, selector):
    water_mols = [(mol_idx, mol) for (mol_idx, mol) in enumerate(system.molecules) if selector(mol)]
    mol_idxs, water_mols = zip(*water_mols)
    positions = []
    for mol in water_mols:
        # TODO: Select CoM vs CoG (current) from FF settings/metavars?
        # TODO: What happens if no atom has a position?
        position = np.average([mol.nodes[n_idx]['position']
                               for n_idx in mol
                               if selector_has_position(mol.nodes[n_idx])], axis=0)
        positions.append(position)
    positions = np.array(positions)
    clust_size = 4  # TODO: Fetch from mapping or FF
    num_clusters = int(np.ceil(len(water_mols)/clust_size))
    cost, clusters, memberships, niter = constrained_kmeans(
        data=positions,
        num_clusters=num_clusters,
        clust_sizes=[clust_size]*num_clusters,
        tolerances=1,
        init_clusters='fixed'
    )
    for mol_idx in sorted(mol_idxs, reverse=True):
        del system.molecules[mol_idx]

    for clust_idx in range(memberships.shape[1]):
        members = memberships[:, clust_idx]
        mols = [mol for (mol, val) in zip(water_mols, members) if val]
        union = Molecule(meta=mols[0].meta, force_field=mols[0].force_field,
                         nrexcl=mols[0].nrexcl)
        for mol in mols:
            union.merge_molecule(mol)
        system.add_molecule(union)


class MoleculeGrouper(Processor):
    def __init__(self):
        self.selector = is_water

    def run_system(self, system):
        group_molecules(system, self.selector)
