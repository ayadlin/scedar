import numpy as np

import scipy.cluster.hierarchy as sph
import scipy.spatial as spspatial

import sklearn as skl
import sklearn.metrics
import sklearn.manifold
from sklearn.neighbors import kneighbors_graph
import sklearn.preprocessing

import warnings

import random

import networkx as nx
from fa2 import ForceAtlas2

import scedar
from .plot import cluster_scatter, heatmap, hist_dens_plot, networkx_graph
from .sfm import SampleFeatureMatrix
from .mdl import MultinomialMdl
from .. import utils
from . import mtype


class SampleDistanceMatrix(SampleFeatureMatrix):
    """
    SampleDistanceMatrix: data with pairwise distance matrix

    Parameters
    ----------
    x : ndarray or list
        data matrix (n_samples, n_features)
    d : ndarray or list or None
        distance matrix (n_samples, n_samples)
        If is None, d will be computed with x, metric, and nprocs.
    metric : string
        distance metric
    sids : homogenous list of int or string
        sample ids. Should not contain duplicated elements.
    fids : homogenous list of int or string
        feature ids. Should not contain duplicated elements.
    nprocs : int
        the number of processes for computing pairwise distance matrix

    Attributes
    ----------
    _x : ndarray
        data matrix (n_samples, n_features)
    _d : ndarray
        distance matrix (n_samples, n_samples)
    _metric : string
        distance metric
    _sids : ndarray
        sample ids.
    _fids : ndarray
        sample ids.
    _tsne_lut: dict
        lookup table for previous tsne calculations. Each run has an
        indexed entry, {(param_str, index) : tsne_res}
    _last_tsne: float array
        The last *stored* tsne results. In no tsne performed before, a run
        with default parameters will be performed.
    _knn_ng_lut: dict
        {(k, aff_scale): knn_graph}
    """

    def __init__(self, x, d=None, metric="correlation",
                 sids=None, fids=None, nprocs=None):
        super(SampleDistanceMatrix, self).__init__(x=x, sids=sids, fids=fids)

        if d is not None:
            try:
                d = np.array(d, dtype="float64")
            except ValueError as e:
                raise ValueError("d must be float. {}".format(e))

            if ((d.ndim != 2) or
                (d.shape[0] != d.shape[1]) or
                (d.shape[0] != self._x.shape[0])):
                # check provided distance matrix shape
                raise ValueError("d should have shape (n_samples, n_samples)")

            d = self.num_correct_dist_mat(d)
        else:
            if metric == "precomputed":
                raise ValueError("metric cannot be precomputed when "
                                 "d is None.")
        if nprocs is None:
            self._nprocs = 1
        else:
            self._nprocs = max(int(nprocs), 1)

        self._lazy_load_d = d
        self._tsne_lut = {}
        self._lazy_load_last_tsne = None
        self._metric = metric
        # Sorted distance matrix. Each column ascending from top to bottom.
        self._lazy_load_col_sorted_d = None
        self._lazy_load_col_argsorted_d = None
        self._knn_ng_lut = {}

    def sort_features(self, fdist_metric="correlation"):
        optimal_f_inds = HClustTree.sort_x_by_d(self._x.T, metric=fdist_metric,
                                                nprocs=self._nprocs)
        self._x = self._x[:, optimal_f_inds]
        self._fids = self._fids[optimal_f_inds]
        return

    # numerically correct dmat
    @staticmethod
    def num_correct_dist_mat(dmat, upper_bound=None):
        if ((not isinstance(dmat, np.ndarray)) or
            (dmat.ndim != 2) or
            (dmat.shape[0] != dmat.shape[1])):
            # check distance matrix shape
            raise ValueError("dmat must be a 2D (n_samples, n_samples)"
                             " np array")

        try:
            # Distance matrix diag vals should be close to 0.
            np.testing.assert_allclose(dmat[np.diag_indices(dmat.shape[0])], 0,
                                       atol=1e-5)
        except AssertionError as e:
            warnings.warn("distance matrix might not be numerically "
                          "correct. diag vals "
                          "should be close to 0. {}".format(e))

        try:
            # distance matrix should be approximately symmetric
            np.testing.assert_allclose(dmat[np.triu_indices_from(dmat)],
                                       dmat.T[np.triu_indices_from(dmat)],
                                       rtol=0.001)
        except AssertionError as e:
            warnings.warn("distance matrix might not be numerically "
                          "correct. should be approximately "
                          "symmetric. {}".format(e))

        dmat[dmat < 0] = 0
        dmat[np.diag_indices(dmat.shape[0])] = 0
        if upper_bound is not None:
            upper_bound = float(upper_bound)
            dmat[dmat > upper_bound] = upper_bound

        dmat[np.triu_indices_from(dmat)] = dmat.T[np.triu_indices_from(dmat)]
        return dmat

    def put_tsne(self, str_params, res):
        """
        Put t-SNE results into the lookup table.
        """
        if type(str_params) != str:
            raise ValueError("Unknown key type: {}".format(str_params))
        curr_store_ind = len(self._tsne_lut) + 1
        tsne_params_key = (str_params, curr_store_ind)
        res_copy = res.copy()
        self._tsne_lut[tsne_params_key] = res_copy
        self._lazy_load_last_tsne = res_copy

    def get_tsne_kv(self, key):
        """
        Get t-SNE results from the lookup table. Return None if non-existent.

        Returns
        -------
        res_tuple: tuple
            (key, val) pair of tsne result.
        """
        if type(key) == int:
            # tuple key (param_str, ind)
            for tk in self._tsne_lut:
                if key == tk[1]:
                    return (tk, self._tsne_lut[tk])
        elif type(key) == str:
            for tk in self._tsne_lut:
                if key == tk[0]:
                    return (tk, self._tsne_lut[tk])
        else:
            raise ValueError("Unknown key type: {}".format(key))
        # key cannot be found
        return None

    def tsne(self, store_res=True, **kwargs):
        """
        Run t-SNE on distance matrix.

        Parameters
        ----------
        store_res: bool
            Store the results in lookup table or not.
        **kwargs
            Keyword arguments passed to tsne computation.

        Returns
        -------
        tsne_res: float array
            t-SNE projections, (n_samples, m dimensions).
        """
        # TODO: make parameter keys consistent such that same set of
        # parameters but different order will sill be the same.
        # check input args
        if ("metric" in kwargs
            and kwargs["metric"] not in ("precomputed", self._metric)):
            raise ValueError("If you want to calculate t-SNE of a different "
                             "metric than the instance metric, create another "
                             "instance of the desired metric.")
        else:
            kwargs["metric"] = "precomputed"
        str_params = str(kwargs)
        tsne_kv = self.get_tsne_kv(str_params)
        if tsne_kv is None:
            if self._x.shape[0] == 0:
                tsne_res = np.empty((0, 0))
            elif self._x.shape[0] == 1:
                tsne_res = np.zeros((1, 2))
            else:
                tsne_res = tsne(self._d, **kwargs)
        else:
            tsne_res = tsne_kv[1]
        if store_res:
            self.put_tsne(str_params, tsne_res)

        return tsne_res

    def par_tsne(self, param_list, store_res=True, nprocs=1):
        """
        Run t-SNE with multiple sets of parameters parallely.

        Parameters
        ----------
        param_list: list of dict
            List of parameters being passed to t-SNE.
        nprocs: int
            Number of processes.

        Returns
        -------
        tsne_res_list: list of float arrays
            List of t-SNE results of corresponding parameter set.

        Notes
        -----
        Parallel running results cannot be stored during the run, because
        racing conditions may happen.
        """
        nprocs = min(int(nprocs), len(param_list))

        f = lambda param_dict: self.tsne(store_res=False, **param_dict)
        if nprocs <= 1:
            resl = list(map(f, param_list))
        else:
            resl = utils.parmap(f, param_list, nprocs)
        if store_res:
            for i in range(len(param_list)):
                self.put_tsne(str(param_list[i]), resl[i])
        return resl

    def tsne_gradient_plot(self, gradient=None, labels=None,
                           selected_labels=None,
                           shuffle_label_colors=False,
                           title=None, xlab=None, ylab=None,
                           figsize=(20, 20), add_legend=True,
                           n_txt_per_cluster=3, alpha=1, s=0.5,
                           random_state=None, **kwargs):
        """
        Plot the last t-SNE projection with the provided gradient as color.
        Gradient is None by default.
        """
        # labels are checked in cluster_scatter
        return cluster_scatter(self._last_tsne, labels=labels,
                               selected_labels=selected_labels,
                               shuffle_label_colors=shuffle_label_colors,
                               gradient=gradient,
                               title=title, xlab=xlab, ylab=ylab,
                               figsize=figsize, add_legend=add_legend,
                               n_txt_per_cluster=n_txt_per_cluster,
                               alpha=alpha, s=s, random_state=random_state,
                               **kwargs)

    def tsne_feature_gradient_plot(self, fid, transform=None, labels=None,
                                   selected_labels=None,
                                   shuffle_label_colors=False,
                                   title=None, xlab=None, ylab=None,
                                   figsize=(20, 20), add_legend=True,
                                   n_txt_per_cluster=3, alpha=1, s=0.5,
                                   random_state=None, **kwargs):
        """
        Plot the last t-SNE projection with the provided gradient as color.

        Parameters
        ----------
        fid: feature id scalar
            ID of the feature to be used for gradient plot.
        transform: callable
            Map transform on feature before plotting.
        labels: label array
            Labels assigned to each point, (n_samples,).
        selected_labels: label array
            Show gradient only for selected labels. Do not show non-selected.
        """
        if mtype.is_valid_sfid(fid):
            fid = [fid]
            f_ind = self.f_id_to_ind(fid)[0]
        else:
            raise ValueError("Invalid fid {}."
                             "Currently only support 1 "
                             "feature gradient plot.".format(fid))

        fx = self._x[:, f_ind]
        if transform is not None:
            if callable(transform):
                fx = np.array(list(map(transform, fx)))
            else:
                raise ValueError("transform must be a callable")

        if labels is not None and len(labels) != fx.shape[0]:
            raise ValueError("labels ({}) must have same length as "
                             "n_samples.".format(labels))

        return cluster_scatter(self._last_tsne, labels=labels,
                               selected_labels=selected_labels,
                               shuffle_label_colors=shuffle_label_colors,
                               gradient=fx,
                               title=title, xlab=xlab, ylab=ylab,
                               figsize=figsize, add_legend=add_legend,
                               n_txt_per_cluster=n_txt_per_cluster,
                               alpha=alpha, s=s, random_state=random_state,
                               **kwargs)

    def ind_x(self, selected_s_inds=None, selected_f_inds=None):
        """
        Subset samples by (sample IDs, feature IDs).

        Parameters
        ----------
        selected_s_inds: int array
            Index array of selected samples. If is None, select all.
        selected_f_inds: int array
            Index array of selected features. If is None, select all.

        Returns
        -------
        subset: SampleDistanceMatrix
        """
        if selected_s_inds is None:
            selected_s_inds = slice(None, None)

        if selected_f_inds is None:
            selected_f_inds = slice(None, None)

        return SampleDistanceMatrix(
            x=self._x[selected_s_inds, :][:, selected_f_inds].copy(),
            d=self._d[selected_s_inds, :][:, selected_s_inds].copy(),
            metric=self._metric,
            sids=self._sids[selected_s_inds].tolist(),
            fids=self._fids[selected_f_inds].tolist(),
            nprocs=self._nprocs)

    def id_x(self, selected_sids=None, selected_fids=None):
        """
        Subset samples by (sample IDs, feature IDs).

        Parameters
        ----------
        selected_s_inds: int array
            Index array of selected samples. If is None, select all.
        selected_f_inds: int array
            Index array of selected features. If is None, select all.

        Returns
        -------
        subset: SampleDistanceMatrix
        """
        if selected_sids is None:
            selected_s_inds = None
        else:
            selected_s_inds = self.s_id_to_ind(selected_sids)

        if selected_fids is None:
            selected_f_inds = None
        else:
            selected_f_inds = self.f_id_to_ind(selected_fids)
        return self.ind_x(selected_s_inds, selected_f_inds)

    def s_ith_nn_d(self, i):
        """
        Computes the distances of the i-th nearest neighbor of all samples.
        """
        return self._col_sorted_d[i, :]

    def s_ith_nn_ind(self, i):
        """
        Computes the sample indices of the i-th nearest neighbor of all
        samples.
        """
        return self._col_argsorted_d[i, :]

    def s_knn_ind_lut(self, k):
        """
        Computes the lookup table for sample i and its KNN indices, i.e.
        `{i : [1st_NN_ind, 2nd_NN_ind, ..., nth_NN_ind], ...}`
        """
        # each column is its KNN index from 1 to k
        if k < 0 or k > self._col_argsorted_d.shape[0] - 1:
            raise ValueError("k ({}) should >= 0 and <= n_samples-1".format(k))
        k = int(k)
        knn_ind_arr = self._col_argsorted_d[1:k+1, :].copy()
        knn_order_ind_lut = dict(zip(range(knn_ind_arr.shape[1]),
                                           knn_ind_arr.T.tolist()))
        return knn_order_ind_lut

    def s_ith_nn_d_dist(self, i, xlab=None, ylab=None, title=None,
                      figsize=(5, 5), ax=None, **kwargs):
        """
        Plot the distances of the i-th nearest neighbor of all samples.
        """
        x = self.s_ith_nn_d(i)
        return hist_dens_plot(x, title=title, xlab=xlab, ylab=ylab,
                              figsize=figsize, ax=ax, **kwargs)


    def s_knn_connectivity_matrix(self, k):
        """
        Computes the connectivity matrix of KNN of samples. If an entry
        `(i, j)` has value 0, node `i` is not in node `j`'s KNN. If an entry
        `(i, j)` has value > 0, node `i` is in `j`'s KNN, and their distance
        is the entry value.

        Parameters
        ----------
        k: int

        Returns
        -------
        knn_conn_mat: float array
            (n_samples, n_samles)
        """
        knn_conn_mat = kneighbors_graph(self._d, k, mode="distance",
                                        metric="precomputed",
                                        include_self=False).toarray()
        return knn_conn_mat

    def draw_s_knn_graph(self, k, aff_scale=1, gradient=None, iterations=2000,
                         figsize=(20, 20), node_size=30, alpha=0.05,
                         random_state=None, init_pos=None,
                         with_labels=False, fa2_kwargs=None,
                         nx_draw_kwargs=None):
        """
        Draw KNN graph of SampleDistanceMatrix. Graph layout using
        forceatlas2 for its speed on large graph.

        Parameters
        ----------
        k: int
        aff_scale: float
            Affinity is calculated by `(max(distance) - distance) * aff_scale`
        gradient: float array
            (n_samples,) color gradient
        iterations: int
            ForceAtlas2 iterations
        figsize: (float, float)
        node_size: float
        alpha: float
        random_state: int
        init_pos: float array
            Initial position of ForceAtlas2, (n_samples, 2).
        with_labels: bool
        fa2_kwargs: dict
        nx_draw_kwargs: dict

        Returns
        -------
        fig: matplotlib figure
            KNN graph.
        """
        # TODO: Docstring. Feature gradient.
        # (n_samples, n_samples). Non-neighbor entries are 0.
        knn_ng_param_key = (k, aff_scale)
        if knn_ng_param_key in self._knn_ng_lut:
            ng = self._knn_ng_lut[knn_ng_param_key]
        else:
            knn_d_arr = self.s_knn_connectivity_matrix(k)
            # Undirected graph
            ng = nx.Graph()
            # affinity shoud negatively correlate to distance
            aff_mat = (knn_d_arr.max() - knn_d_arr) * aff_scale
            # Add graph edges
            # Nodes are in the same order with their indices
            for i in range(knn_d_arr.shape[0]):
                for j in range(i+1, knn_d_arr.shape[0]):
                    if knn_d_arr[i, j] > 0:
                        ng.add_edge(self._sids[i], self._sids[j],
                                    weight=aff_mat[i, j])
            self._knn_ng_lut[knn_ng_param_key] = ng.copy()

        if fa2_kwargs is None:
            fa2_kwargs = {}
        else:
            fa2_kwargs = fa2_kwargs.copy()
        random.seed(random_state)
        forceatlas2 = ForceAtlas2(
            # Dissuade hubs
            outboundAttractionDistribution=fa2_kwargs.pop(
                "outboundAttractionDistribution", True),
            edgeWeightInfluence=fa2_kwargs.pop("edgeWeightInfluence", 1.0),
            # Performance
            # Tolerance
            jitterTolerance=fa2_kwargs.pop("jitterTolerance", 1.0),
            barnesHutOptimize=fa2_kwargs.pop("barnesHutOptimize", True),
            barnesHutTheta=fa2_kwargs.pop("barnesHutTheta", 1.2),
            # Tuning
            scalingRatio=fa2_kwargs.pop("scalingRatio", 2.0),
            strongGravityMode=fa2_kwargs.pop("strongGravityMode", True),
            gravity=fa2_kwargs.pop("gravity", 1.0),
            # Log
            verbose=fa2_kwargs.pop("verbose", False), **fa2_kwargs)
        knn_fa2pos = forceatlas2.forceatlas2_networkx_layout(
            ng, pos=init_pos, iterations=iterations)

        if nx_draw_kwargs is None:
            nx_draw_kwargs = {}
        else:
            nx_draw_kwargs = nx_draw_kwargs.copy()
        if gradient is None:
            node_color = nx_draw_kwargs.pop("node_color", "b")
            cmap = nx_draw_kwargs.pop("cmap", None)
        else:
            node_color = gradient
            cmap = nx_draw_kwargs.pop("cmap", "viridis")

        fig = networkx_graph(ng, knn_fa2pos, alpha=alpha, figsize=figsize,
                             node_color=node_color, node_size=node_size,
                             cmap=cmap, with_labels=with_labels,
                             **nx_draw_kwargs)
        return fig

    @property
    def d(self):
        return self._d.tolist()

    @property
    def _d(self):
        # TODO: Implement corr/cosine distance computation using numpy
        # matrix operations. Extremely faster than pdist and pairwise_distance.
        if self._lazy_load_d is None:
            if self._x.size == 0:
                self._lazy_load_d = np.zeros((self._x.shape[0],
                                              self._x.shape[0]))
            else:
                self._lazy_load_d = self.num_correct_dist_mat(
                    skl.metrics.pairwise.pairwise_distances(
                        self._x, metric=self._metric, n_jobs=self._nprocs))
        return self._lazy_load_d

    @property
    def _col_sorted_d(self):
        # Use mergesort to be stable
        if self._lazy_load_col_sorted_d is None:
            self._lazy_load_col_sorted_d = np.sort(self._d,
                                                   kind="mergesort",
                                                   axis=0)
        return self._lazy_load_col_sorted_d

    @property
    def _col_argsorted_d(self):
        # Use mergesort to be stable
        if self._lazy_load_col_argsorted_d is None:
            self._lazy_load_col_argsorted_d = np.argsort(self._d,
                                                         kind="mergesort",
                                                         axis=0)
        return self._lazy_load_col_argsorted_d

    @property
    def _last_tsne(self):
        if self._lazy_load_last_tsne is None:
            self._lazy_load_last_tsne = self.tsne()
        return self._lazy_load_last_tsne

    @property
    def metric(self):
        return self._metric

    @property
    def tsne_lut(self):
        return dict((key, val.copy()) for key, val in self._tsne_lut.items())


# x : (n_samples, n_features) or (n_samples, n_samples)
# If metric is 'precomputed', x must be a pairwise distance matrix
def tsne(x, n_components=2, perplexity=30.0, early_exaggeration=12.0,
         learning_rate=200.0, n_iter=1000, n_iter_without_progress=300,
         min_grad_norm=1e-07, metric="euclidean", init="random", verbose=0,
         random_state=None, method="barnes_hut", angle=0.5):
    x_tsne = sklearn.manifold.TSNE(
        n_components=n_components, perplexity=perplexity,
        early_exaggeration=early_exaggeration,
        learning_rate=learning_rate, n_iter=n_iter,
        n_iter_without_progress=n_iter_without_progress,
        min_grad_norm=min_grad_norm, metric=metric,
        init=init, verbose=verbose,
        random_state=random_state, method=method,
        angle=angle).fit_transform(x)
    return x_tsne


class HClustTree(object):
    """
    Hierarchical clustering tree.

    Implement simple tree operation routines. HCT is binary unbalanced tree.

    Attributes
    ----------
    node : scipy.cluster.hierarchy.ClusterNode
        current node
    prev : HClustTree
        parent of current node

    """

    def __init__(self, node, prev=None):
        super(HClustTree, self).__init__()
        self._node = node
        self._prev = prev

        if node is None:
            left = None
            right = None
        else:
            left = node.get_left()
            right = node.get_right()

        self._left = left
        self._right = right

    @property
    def prev(self):
        return self._prev

    def count(self):
        if self._node is None:
            return 0
        else:
            return self._node.get_count()

    def left(self):
        return HClustTree(self._left, self)

    def left_count(self):
        return self.left().count()

    def right(self):
        return HClustTree(self._right, self)

    def right_count(self):
        return self.right().count()

    def leaf_ids(self):
        if self._node is None:
            return None
        else:
            return self._node.pre_order(lambda xn: xn.get_id())

    def left_leaf_ids(self):
        return self.left().leaf_ids()

    def right_leaf_ids(self):
        return self.right().leaf_ids()

    def bi_partition(self, return_subtrees=False):
        labs, sids = self.cluster_id_to_lab_list([self.left_leaf_ids(),
                                                        self.right_leaf_ids()],
                                                       self.leaf_ids())
        if return_subtrees:
            return labs, sids, self.left(), self.right()
        else:
            return labs, sids

    def n_round_bipar_cnt(self, n):
        assert n > 0
        nr_bipar_cnt_list = []
        curr_hct_list = [self]
        curr_hct_cnt_list = []
        next_hct_list = []
        for i in range(n):
            for iter_hct in curr_hct_list:
                iter_left = iter_hct.left()
                iter_right = iter_hct.right()
                next_hct_list += [iter_left, iter_right]
                curr_hct_cnt_list += [iter_left.count(), iter_right.count()]
            nr_bipar_cnt_list.append(curr_hct_cnt_list)

            curr_hct_list = next_hct_list
            next_hct_list = []
            curr_hct_cnt_list = []
        return nr_bipar_cnt_list

    @staticmethod
    def cluster_id_to_lab_list(cl_sid_list, sid_list):
        """
        Convert nested clustered ID list into cluster label list.

        For example, convert `[[0, 1, 2], [3,4]]` to `[0, 0, 0, 1, 1]`
        according to id_arr `[0, 1, 2, 3, 4]`

        Parameters

        cl_sid_list: list[list[id]]
            Nested list with each sublist as a sert of IDs from a cluster.
        sid_list: list[id]
            Flat list of lists.

        """

        # checks uniqueness
        # This guarantees that clusters are all non-empty
        mtype.check_is_valid_sfids(sid_list)

        if type(cl_sid_list) != list:
            raise ValueError(
                "cl_sid_list must be a list: {}".format(cl_sid_list))

        for x in cl_sid_list:
            mtype.check_is_valid_sfids(x)

        cl_id_mlist = np.concatenate(cl_sid_list).tolist()
        mtype.check_is_valid_sfids(cl_id_mlist)

        # np.unique returns sorted unique values
        if sorted(sid_list) != sorted(cl_id_mlist):
            raise ValueError(
                "sid_list should have the same ids as cl_sid_list.")

        cl_ind_lut = {}
        # iter_cl_ind : cluster index
        # iter_cl_sids: individual cluster list
        for iter_cl_ind, iter_cl_sids in enumerate(cl_sid_list):
            for sid in iter_cl_sids:
                cl_ind_lut[sid] = iter_cl_ind

        lab_list = [cl_ind_lut[x] for x in sid_list]
        return lab_list, sid_list

    @staticmethod
    def hclust_tree(dmat, linkage="complete", n_eval_rounds=None,
                    is_euc_dist=False, optimal_ordering=False, verbose=False):
        dmat = np.array(dmat, dtype="float")
        dmat = SampleDistanceMatrix.num_correct_dist_mat(dmat)

        n = dmat.shape[0]

        if linkage == "auto":
            try_linkages = ("single", "complete", "average", "weighted")

            if is_euc_dist:
                try_linkages += ("centroid", "median", "ward")

            if n_eval_rounds is None:
                n_eval_rounds = int(np.ceil(np.log2(n)))
            else:
                n_eval_rounds = int(np.ceil(max(np.log2(n), n_eval_rounds)))

            ltype_mdl_list = []
            for iter_ltype in try_linkages:
                iter_lhct = HClustTree.hclust_tree(dmat, linkage=iter_ltype)
                iter_nbp_cnt_list = iter_lhct.n_round_bipar_cnt(n_eval_rounds)
                iter_nbp_mdl_arr = np.array(list(map(
                    lambda x: MultinomialMdl(np.array(x)).mdl,
                    iter_nbp_cnt_list)))
                iter_nbp_mdl = np.sum(iter_nbp_mdl_arr
                                      / np.arange(1, n_eval_rounds + 1))
                ltype_mdl_list.append(iter_nbp_mdl)

            linkage = try_linkages[ltype_mdl_list.index(max(ltype_mdl_list))]

            if verbose:
                print(linkage, tuple(zip(try_linkages, ltype_mdl_list)),
                      sep="\n")

        dmat_sf = spspatial.distance.squareform(dmat)
        hac_z = sph.linkage(dmat_sf, method=linkage,
                            optimal_ordering=optimal_ordering)
        return HClustTree(sph.to_tree(hac_z))

    @staticmethod
    def sort_x_by_d(x, dmat=None, metric="correlation", linkage="auto",
                    n_eval_rounds=None, nprocs=None, verbose=False):
        dmat = SampleDistanceMatrix(x, d=dmat, metric=metric,
                                    nprocs=nprocs)._d
        xhct = HClustTree.hclust_tree(dmat, linkage="auto",
            is_euc_dist=(metric == "euclidean"), optimal_ordering=True)
        return xhct.leaf_ids()
