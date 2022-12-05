"""
Classes to get, save and load (static or time dependent) connection matrices and sample submatrices from them
authors: Michael Reimann, András Ecker
last modified: 01.2022
"""

import h5py
import numpy as np
import pandas as pd
from scipy import sparse

from .circuit_models.neuron_groups.defaults import GID
from .circuit_models.connection_matrix import LOCAL_CONNECTOME

_MAT_GLOBAL_INDEX = 0


class _MatrixNodeIndexer(object):
    def __init__(self, parent, prop_name):
        self._parent = parent
        self._prop = parent._vertex_properties[prop_name]
        if isinstance(self._prop.dtype, int) or isinstance(self._prop.dtype, float):
            self.random = self.random_numerical
        else:
            self.random = self.random_categorical

    def eq(self, other):
        pop = self._parent._vertex_properties.index.values[self._prop == other]
        return self._parent.subpopulation(pop)

    def isin(self, other):
        pop = self._parent._vertex_properties.index.values[np.in1d(self._prop, other)]
        return self._parent.subpopulation(pop)

    def le(self, other):
        pop = self._parent._vertex_properties.index.values[self._prop <= other]
        return self._parent.subpopulation(pop)

    def lt(self, other):
        pop = self._parent._vertex_properties.index.values[self._prop < other]
        return self._parent.subpopulation(pop)

    def ge(self, other):
        pop = self._parent._vertex_properties.index.values[self._prop >= other]
        return self._parent.subpopulation(pop)

    def gt(self, other):
        pop = self._parent._vertex_properties.index.values[self._prop > other]
        return self._parent.subpopulation(pop)

    def random_numerical_gids(self, ref, n_bins=50):
        all_gids = self._prop.index.values
        ref_gids = self._parent.__extract_vertex_ids__(ref)
        assert np.isin(ref_gids, all_gids).all(), "Reference gids are not part of the connectivity matrix"

        ref_values = self._prop[ref_gids]
        hist, bin_edges = np.histogram(ref_values.values, bins=n_bins)
        bin_edges[-1] += (bin_edges[-1] - bin_edges[-2]) / 1E9
        value_bins = np.digitize(self._prop.values, bins=bin_edges)
        assert len(hist == len(value_bins[1:-1]))  # `digitize` returns values below and above the spec. bin_edges
        sample_gids = []
        for i in range(n_bins):
            idx = np.where(value_bins == i+1)[0]
            assert idx.shape[0] >= hist[i], "Not enough neurons at this depths to sample from"
            sample_gids.extend(np.random.choice(all_gids[idx], hist[i], replace=False).tolist())
        return sample_gids

    def random_numerical(self, ref, n_bins=50):
        return self._parent.subpopulation(self.random_numerical_gids(ref, n_bins))

    def random_categorical_gids(self, ref):
        all_gids = self._prop.index.values
        ref_gids = self._parent.__extract_vertex_ids__(ref)
        assert np.isin(ref_gids, all_gids).all(), "Reference gids are not part of the connectivity matrix"

        ref_values = self._prop[ref_gids].values
        value_lst, counts = np.unique(ref_values, return_counts=True)
        sample_gids = []
        for i, value in enumerate(value_lst):
            idx = np.where(self._prop == value)[0]
            assert idx.shape[0] >= counts[i], "Not enough %s to sample from" % value
            sample_gids.extend(np.random.choice(all_gids[idx], counts[i], replace=False).tolist())
        return sample_gids

    def random_categorical(self, ref):
        return self._parent.subpopulation(self.random_categorical_gids(ref))


class _MatrixEdgeIndexer(object):
    def __init__(self, parent, prop_name):
        self._parent = parent
        self._prop = parent.edges[prop_name].values

    def eq(self, other):
        idxx = self._prop == other
        return self._parent.subedges(idxx)

    def isin(self, other):
        idxx = np.isin(self._prop, other)
        return self._parent.subedges(idxx)

    def le(self, other):
        idxx = self._prop <= other
        return self._parent.subedges(idxx)

    def lt(self, other):
        idxx = self._prop < other
        return self._parent.subedges(idxx)

    def ge(self, other):
        idxx = self._prop >= other
        return self._parent.subedges(idxx)

    def gt(self, other):
        idxx = self._prop > other
        return self._parent.subedges(idxx)

    def full_sweep(self, direction='decreasing'):
        #  For an actual filtration. Take all values and sweep
        raise NotImplementedError()
    
    def random_by_vertex_property_ids(self, ref, prop_name, n_bins=None, is_edges=False):
        if isinstance(ref, ConnectivityMatrix):
            assert np.all(np.in1d(ref.gids, self._parent.gids))
        else:
            if is_edges:
                ref = self._parent.subedges(ref)
            else:
                try:
                    ref = self._parent.subpopulation(self._parent.__extract_vertex_ids__(ref))
                    print("Interpreting reference as vertex ids. If that is wrong, set is_edges=True")
                except (AssertionError, IndexError):
                    ref = self._parent.subedges(ref)
                    print("Interpreting reference as edge ids!")

        ref_edges = ref.edge_associated_vertex_properties(prop_name)
        parent_edges = self._parent.edge_associated_vertex_properties(prop_name)

        if n_bins is not None:
            mn, mx = np.min(parent_edges.values.flat), np.max(parent_edges.values.flat)
            bins = np.linspace(mn, mx + (mx - mn) / 1E9, n_bins + 1)
            ref_edges = ref_edges.apply(np.digitize, axis=0, bins=bins)
            parent_edges = parent_edges.apply(np.digitize, axis=0, bins=bins)

        ref_counts = ref_edges.value_counts()
        parent_edges = parent_edges.reset_index().set_index(["row", "col"])["index"]

        out_edges = []
        for _idx, n in ref_counts.iteritems():
            out_edges.extend(np.random.choice(parent_edges[_idx].values, n, replace=False))
        return out_edges
    
    def random_by_vertex_property(self, ref, prop_name, n_bins=None):
        edge_ids = self.random_by_vertex_property_ids(ref, prop_name, n_bins=n_bins)
        return self._parent.subedges(edge_ids)


class _MatrixNeighborhoodIndexer(object):

    def __init__(self, parent):
        self._parent = parent
        self._prop = parent._lookup
    
    def get_single(self, pre=None, post=None, center_first=True):
        if pre is None and post is None: raise ValueError("Insufficient number of arguments!")

        indexer = self._parent._edge_indices.reset_index()
        idxx = set()
        centers = []
        if pre is not None:
            centers.append(self._prop[pre])
            idxx = idxx.union(indexer.set_index("row")["col"].get(centers[-1:], []))
        if post is not None:
            if pre != post:
                centers.append(self._prop[post])
            idxx = idxx.union(indexer.set_index("col")["row"].get(centers[-1:], []))
        if center_first:
            pop_ids = self._parent._vertex_properties.index[centers + sorted(idxx)]
        else:
            idxx = idxx.union(centers)
            pop_ids = self._parent._vertex_properties.index[sorted(idxx)]
        return self._parent.subpopulation(pop_ids)
        
    def get(self, *args, pre=None, post=None, center_first=True):
        if len(args) > 1:
            raise ValueError("Please provide a single vertex identifier or use the kwargs!")
        if len(args) == 1:
            arg = args[0]
            if hasattr(arg, "__iter__"):
                mats = [self.get_single(pre=_arg, post=_arg, center_first=center_first) for _arg in arg]
                df = pd.DataFrame({"center": arg})
                return ConnectivityGroup(df, mats)
            return self.get_single(pre=arg, post=arg, center_first=center_first)
        if not hasattr(pre, "__iter__"):
            if not hasattr(post, "__iter__"):
                return self.get_single(pre, post, center_first=center_first)
            pre = [pre for _ in post]
        if not hasattr(post, "__iter__"): post = [post for _ in pre]
        assert len(pre) == len(post), "Argument mismatch!"
        mats = [self.get_single(_pre, _post, center_first=center_first) for _pre, _post in zip(pre, post)]
        df = pd.DataFrame({"center_pre": pre, "center_post": post})
        return ConnectivityGroup(df, mats)

    def __getitem__(self, idx):
        return self.get(idx)


class ConnectivityMatrix(object):
    """Class to get, save, load and hold a connections matrix and generate submatrices from it"""
    def __init__(self, *args, vertex_labels=None, vertex_properties=None,
                 edge_properties=None, default_edge_property="data", shape=None):
        """Not too intuitive init - please see `from_bluepy()` below"""
        """Initialization 1: By adjacency matrix"""
        if len(args) == 1 and isinstance(args[0], np.ndarray) or isinstance(args[0], sparse.spmatrix):
            m = args[0]
            assert m.ndim == 2
            if isinstance(args[0], sparse.spmatrix):
                m = m.tocoo()  # Does not copy data if it already is coo
            else:
                m = sparse.coo_matrix(m)
            self._edges = pd.DataFrame({
                'data': m.data
            })
            if shape is None: shape = m.shape
            else: assert shape == m.shape
            self._edge_indices = pd.DataFrame({
                "row": m.row,
                "col": m.col
            })
            # Adding additional edge properties
            if edge_properties is not None:
                for prop_name, prop_mat in edge_properties.items():
                    self.add_edge_property(prop_name, prop_mat)
        else:
            if len(args) >= 2:
                assert len(args[0]) == len(args[1])
                df = pd.DataFrame({
                    "row": args[0],
                    "col": args[1]
                })
            else:
                df = args[0]
            """Initialization 2: By edge-specific DataFrames"""
            assert edge_properties is not None
            edge_properties = pd.DataFrame(edge_properties)  # In case input is dict
            assert len(edge_properties) == len(df)
            self._edge_indices = df

            if shape is None: 
                shape = tuple(df.max(axis=0).values + 1)
            self._edges = edge_properties
            if default_edge_property not in self.edges:
                default_edge_property = edge_properties.columns[0]  # Or exception?

        # In the future: implement the ability to represent connectivity from population A to B.
        # For now only connectivity within one and the same population
        assert shape[0] == shape[1]
        self._shape = shape
        self.__inititalize_vertex_properties__(vertex_labels, vertex_properties)

        self._default_edge = default_edge_property

        self._lookup = self.__make_lookup__()
        #  NOTE: This part implements the .gids and .depth properties
        for colname in self._vertex_properties.columns:
            #  TODO: Check colname against existing properties
            setattr(self, colname, self._vertex_properties[colname].values)

        # TODO: calling it "gids" might be too BlueBrain-specific! Change name?
        self.gids = self._vertex_properties.index.values
        # TODO: Additional tests, such as no duplicate edges!
        self.neighborhood = _MatrixNeighborhoodIndexer(self)

    def __len__(self):
        return len(self.gids)

    def add_vertex_property(self, new_label, new_values):
        assert len(new_values) == len(self), "New values size mismatch"
        assert new_label not in self._vertex_properties, "Property {0} already exists!".format(new_label)
        self._vertex_properties[new_label] = new_values
    
    def add_edge_property(self, new_label, new_values):
        if (isinstance(new_values, np.ndarray) and new_values.ndim == 2) or isinstance(new_values, sparse.spmatrix):
            if isinstance(new_values, sparse.spmatrix):
                new_values = new_values.tocoo()
            else:
                new_values = sparse.coo_matrix(new_values)
            # TODO: Reorder data instead of throwing exception
            assert np.all(new_values.row == self._edge_indices["row"]) and np.all(new_values.col == self._edge_indices["col"])
            self._edges[new_label] = new_values.data
        else:
            if hasattr(new_values, "values"):
                new_values = new_values.values
            assert len(new_values) == len(self._edge_indices)
            self._edges[new_label] = new_values
    
    def __inititalize_vertex_properties__(self, vertex_labels, vertex_properties):
        if vertex_properties is None:
            if vertex_labels is None:
                vertex_labels = np.arange(self._shape[0])
            self._vertex_properties = pd.DataFrame({}, index=vertex_labels)
        elif isinstance(vertex_properties, dict):
            if vertex_labels is None:
                vertex_labels = np.arange(self._hape[0])
            self._vertex_properties = pd.DataFrame(vertex_properties, index=vertex_labels)
        elif isinstance(vertex_properties, pd.DataFrame):
            if vertex_labels is not None:
                raise ValueError("""Cannot specify vertex labels separately
                                 when instantiating vertex_properties explicitly""")
            self._vertex_properties = vertex_properties
        else:
            raise ValueError("""When specifying vertex properties provide a DataFrame or dict""")
        assert len(self._vertex_properties) == self._shape[0]

    def __make_lookup__(self):
        return pd.Series(np.arange(self._shape[0]), index=self._vertex_properties.index)

    def matrix_(self, edge_property=None):
        if edge_property is None:
            edge_property = self._default_edge
        return sparse.coo_matrix((self.edges[edge_property], (self._edge_indices["row"], self._edge_indices["col"])),
                                 shape=self._shape, copy=False)
    @property
    def edges(self):
        return self._edges
    
    @property
    def vertices(self):
        return self._vertex_properties.reset_index()

    @property
    def edge_properties(self):
        # TODO: Maybe add 'row' and 'col'?
        return self.edges.columns.values

    @property
    def vertex_properties(self):
        return self._vertex_properties.columns.values
    
    def edge_associated_vertex_properties(self, prop_name):
        assert prop_name in self.vertex_properties, "{0} is not a vertex property: {1}".format(prop_name, self.vertex_properties)
        eavp = pd.concat(
            [self.vertices[prop_name][self._edge_indices[_idx]].rename(_idx).reset_index(drop=True)
             for _idx in self._edge_indices.columns],
             axis=1, copy=False
        )
        return eavp
    
    def matrix_(self, edge_property=None):
        if edge_property is None:
            edge_property = self._default_edge
        return sparse.coo_matrix((self.edges[edge_property], (self._edge_indices["row"], self._edge_indices["col"])),
                                 shape=self._shape)

    @property
    def matrix(self):
        return self.matrix_(self._default_edge)

    def dense_matrix_(self, edge_property=None):
        return self.matrix_(edge_property=edge_property).todense()

    @property
    def dense_matrix(self):
        return self.dense_matrix_()

    def array_(self, edge_property=None):
        return np.array(self.dense_matrix_(edge_property=edge_property))

    @property
    def array(self):
        return self.array_()

    def index(self, prop_name):
        assert prop_name in self._vertex_properties, "vertex property should be in " + str(self.vertex_properties)
        return _MatrixNodeIndexer(self, prop_name)

    def filter(self, prop_name=None):
        if prop_name is None:
            prop_name = self._default_edge
        return _MatrixEdgeIndexer(self, prop_name)

    def default(self, new_default_property, copy=True):
        assert new_default_property in self.edge_properties, "Edge property {0} unknown!".format(new_default_property)
        if not copy:
            self._default_edge = new_default_property
            return self
        return self.__class__(self._edge_indices["row"], self._edge_indices["col"],
                                  edge_properties=self._edges,
                                  vertex_properties=self._vertex_properties, shape=self._shape,
                                  default_edge_property=new_default_property)

    @staticmethod
    def __extract_vertex_ids__(an_obj):
        if hasattr(an_obj, GID):
            return getattr(an_obj, GID)
        return an_obj

    @classmethod
    def from_bluepy(cls, bluepy_obj, load_config=None, gids=None, connectome=LOCAL_CONNECTOME):
        """
        BlueConfig/CircuitConfig based constructor
        :param bluepy_obj: bluepy Simulation or Circuit object
        :param load_config: config dict for loading and filtering neurons from the circuit
        :param gids: array of gids AKA. the nodes of the graph, if not None: the intersection of these gids
                     and the ones loaded based on the `load_config` will be used
        :param connectome: str. that can be "local" which specifies local circuit connectome
                           or the name of a projection to use
        """
        from .circuit_models.neuron_groups import load_filter
        from .circuit_models import circuit_connection_matrix

        if hasattr(bluepy_obj, "circuit"):
            circ = bluepy_obj.circuit
        else:
            circ = bluepy_obj
        
        nrn = load_filter(circ, load_config)
        nrn = nrn.set_index(GID)
        # TODO: decide if this extra filtering is needed (or make load_config optional
        #  and implement gid based property loading in circuit_models.neuron_groups)
        if gids is not None:
            nrn = nrn.loc[nrn.index.intersection(gids)]
        # TODO: think a bit about if it should even be possible to call this for a projection (remove arg. if not...)
        # TODO: Add option to look up synapse properties here
        mat = circuit_connection_matrix(circ, for_gids=nrn.index.values, connectome=connectome)
        return cls(mat, vertex_properties=nrn)

    def submatrix(self, sub_gids, edge_property=None, sub_gids_post=None):
        """Return a submatrix specified by `sub_gids`"""
        m = self.matrix_(edge_property=edge_property).tocsc()
        if sub_gids_post is not None:
            return m[np.ix_(self._lookup[self.__extract_vertex_ids__(sub_gids)],
                            self._lookup[self.__extract_vertex_ids__(sub_gids_post)])]
        idx = self._lookup[self.__extract_vertex_ids__(sub_gids)]
        return m[np.ix_(idx, idx)]

    def dense_submatrix(self, sub_gids, edge_property=None, sub_gids_post=None):
        return self.submatrix(sub_gids, edge_property=edge_property, sub_gids_post=sub_gids_post).todense()

    def subarray(self, sub_gids, edge_property=None, sub_gids_post=None):
        return np.array(self.dense_submatrix(sub_gids, edge_property=edge_property, sub_gids_post=sub_gids_post))

    def subpopulation(self, subpop_ids):
        """A ConnectivityMatrix object representing the specified subpopulation"""
        subpop_ids = self.__extract_vertex_ids__(subpop_ids)
        assert np.all(np.in1d(subpop_ids, self._vertex_properties.index.values))
        subpop_idx = self._lookup[subpop_ids]
        # TODO: This would be more efficient if the underlying representation was csc.
        # TODO: This fails if there are duplicate entries with the same row/col.
        subindex = sparse.coo_matrix((range(len(self._edge_indices["row"])),
                                    (self._edge_indices["row"], self._edge_indices["col"])),
                                     copy=False, shape=self._shape).tocsc()
        subindex = subindex[np.ix_(subpop_idx, subpop_idx)].tocoo()

        out_edges = self._edges.iloc[subindex.data]
        out_vertices = self._vertex_properties.loc[subpop_ids]
        # TODO: This will result in indices of _edge_indices and _edges by different.
        # That is OK, because the indices are never used. But still, might want to revisit...
        return ConnectivityMatrix(subindex.row, subindex.col, vertex_properties=out_vertices,
                                  edge_properties=out_edges, default_edge_property=self._default_edge,
                                  shape=(len(subpop_ids), len(subpop_ids)))

    def subedges(self, subedge_indices):
        """A ConnectivityMatrix object representing the specified subpopulation"""
        if isinstance(subedge_indices, pd.Series):
            subedge_indices = subedge_indices.values
        rowcol = self._edge_indices.iloc[subedge_indices]
        out_edges = self._edges.iloc[subedge_indices]

        return ConnectivityMatrix(rowcol["row"], rowcol["col"], vertex_properties=self._vertex_properties,
        edge_properties=out_edges, default_edge_property=self._default_edge, shape=self._shape)

    def random_n_gids(self, ref):
        """Randomly samples `ref` number of neurons if `ref` is and int,
        otherwise the same number of neurons as in `ref`"""
        all_gids = self._vertex_properties.index.values
        if hasattr(ref, "__len__"):
            assert np.isin(self.__extract_vertex_ids__(ref),
                           all_gids).all(), "Reference gids are not part of the connectivity matrix"
            n_samples = len(ref)
        elif isinstance(ref, int):  # Just specify the number
            n_samples = ref
        else:
            raise ValueError("random_n_gids() has to be called with an int or something that has len()")
        return np.random.choice(all_gids, n_samples, replace=False)

    def random_n(self, ref):
        return self.subpopulation(self.random_n_gids(ref))
    
    def analyze(self, analysis_recipe):
        from .analysis import get_analyses
        analyses = get_analyses(analysis_recipe)
        res = {}
        for analysis in analyses:
            res[analysis._name] = analysis.apply(self)
        return res

    @classmethod
    def from_h5(cls, fn, group_name=None, prefix=None):
        if prefix is None:
            prefix = "connectivity"
        if group_name is None:
            group_name = "full_matrix"
        full_prefix = prefix + "/" + group_name
        vertex_properties = pd.read_hdf(fn, full_prefix + "/vertex_properties")
        edges = pd.read_hdf(fn, full_prefix + "/edges")
        edge_idx = pd.read_hdf(fn, full_prefix + "/edge_indices")

        with h5py.File(fn, 'r') as h5:
            data_grp = h5[full_prefix]
            shape = tuple(data_grp.attrs["NEUROTOP_SHAPE"])
            def_edge = data_grp.attrs["NEUROTOP_DEFAULT_EDGE"]
        return cls(edge_idx["row"], edge_idx["col"], vertex_properties=vertex_properties, edge_properties=edges,
                   default_edge_property=def_edge, shape=shape)

    def to_h5(self, fn, group_name=None, prefix=None):
        if prefix is None:
            prefix = "connectivity"
        if group_name is None:
            group_name = "full_matrix"
        full_prefix = prefix + "/" + group_name
        self._vertex_properties.to_hdf(fn, key=full_prefix + "/vertex_properties", format="table")
        self._edges.to_hdf(fn, key=full_prefix + "/edges")
        self._edge_indices.to_hdf(fn, key=full_prefix + "/edge_indices")

        with h5py.File(fn, "a") as h5:
            data_grp = h5[full_prefix]
            data_grp.attrs["NEUROTOP_SHAPE"] = self._shape
            data_grp.attrs["NEUROTOP_DEFAULT_EDGE"] = self._default_edge
            data_grp.attrs["NEUROTOP_CLASS"] = "ConnectivityMatrix"


def _update_load_config(load_cfg, sim_tgt):
    from .circuit_models.neuron_groups.grouping_config import _read_if_needed
    load_config = _read_if_needed(load_cfg)
    if load_config is None:
        load_config = {"loading": {"base_target": sim_tgt}}
    elif "loading" in load_config:
        if "base_target" not in load_config["loading"]:
            load_config["loading"]["base_target"] = sim_tgt
    else:  # why is this part necessary?
        load_config["base_target"] = sim_tgt
    return load_config


class StructurallyPlasticMatrix(ConnectivityMatrix):
    def __init__(self, *args, vertex_labels=None, vertex_properties=None,
                 edge_properties=None, default_edge_property="data", shape=None,
                 edge_off={}, edge_on={}, check_consistency=True):
        super().__init__(*args, vertex_labels=vertex_labels, vertex_properties=vertex_properties,
                         edge_properties=edge_properties, default_edge_property=default_edge_property,
                         shape=shape)
        self._off = self._build_on_off_index(edge_off)
        self._on = self._build_on_off_index(edge_on)
        if check_consistency:
            check = self.is_consistent()
            failure_count = (~check).sum()
            if failure_count > 0:
                raise ValueError("On-off data is inconsistent for {0} edges!".format(failure_count))
    
    @staticmethod
    def _build_on_off_index(struc_in):
        if isinstance(struc_in, dict):
            t = sorted(struc_in.keys())
            if len(struc_in) > 0:
                idx = pd.Index(np.hstack([_t * np.ones_like(struc_in[_t]) for _t in t]), name="t", dtype="int64")
                vals = np.hstack([struc_in[_t] for _t in t])
            else:
                idx = pd.Index([], name="t", dtype="int64")
                vals = []
            return pd.Series(vals, index=idx, name="edge", dtype="int64").sort_index()
        elif isinstance(struc_in, pd.Series):
            idx = pd.Index(struc_in.index, dtype="int64", name="t")
            struc_in.index = idx
            struc_in.name = "edge"
            return struc_in.sort_index()
        else:
            raise ValueError()
    
    def __getitem__(self, idx):
        is_off = self._off.loc[:idx].drop_duplicates(keep="last")
        is_on = self._on.loc[:idx].drop_duplicates(keep="last")
        off_mx = is_off.reset_index().groupby("edge").agg("max") # the last time it's switched off
        on_mx = is_on.reset_index().groupby("edge").agg("max") # the last time its' switched_on

        idxx = pd.Series(0, index=range(len(self._edge_indices)), name="t").to_frame()
        idxx = idxx.subtract(off_mx.subtract(on_mx, fill_value=-1), fill_value=0) >= 0

        return self.subedges(idxx.index[idxx["t"]].values)
    
    def delta(self, idx_fr, idx_to):
        mxx = self._off.index.max() + 1
        is_off = self._off.get(np.arange(idx_fr + 1, idx_to + 1), self._off.iloc[:0]).reset_index().groupby("edge")
        is_on = self._on.get(np.arange(idx_fr + 1, idx_to + 1), self._on.iloc[:0]).reset_index().groupby("edge")

        off_mx = is_off.agg("max")
        on_mx = is_on.agg("max")
        off_mn = is_off.agg("min")
        on_mn = is_on.agg("min")

        off_first = off_mn.subtract(on_mn, fill_value=mxx) < 0
        off_last = off_mx.subtract(on_mx, fill_value=-1) > 0
        delta_sign = -((off_first.astype(int) + off_last) - 1)
        delta_sign = delta_sign["t"][delta_sign["t"] != 0]

        ret = self.subedges(delta_sign.index)
        ret.add_edge_property("delta", delta_sign.values)
        return ret.default("delta", copy=False)
    
    def skip(self, step, copy=True):
        new_off = self._off.drop(step)
        new_on = self._on.drop(step)
        if copy:
            return StructurallyPlasticMatrix(self._edge_indices, vertex_properties=self._vertex_properties,
                                             edge_properties=self._edges, default_edge_property=self._default_edge,
                                             shape=self._shape, edge_off=new_off, edge_on=new_on,
                                             check_consistency=False).fix_consistency(copy=False)
        self._off = new_off
        self._on = new_on
        return self.fix_consistency(copy=False)
    
    def count_changes(self, count_off=True, count_on=True):
        counts = pd.DataFrame({"count": np.zeros(len(self._edge_indices), dtype=int)})
        if count_off:
            to_add = self._off.reset_index().groupby("edge").agg(len)
            counts = counts.add(to_add.rename(columns={"t": "count"}), fill_value=0)
        if count_on:
            to_add = self._on.reset_index().groupby("edge").agg(len)
            counts = counts.add(to_add.rename(columns={"t": "count"}), fill_value=0)
        counts["data"] = np.ones(len(self._edge_indices), dtype=bool)
        return ConnectivityMatrix(self._edge_indices, vertex_properties=self._vertex_properties,
                                  edge_properties=counts, default_edge_property="count")
    
    def amount_active(self):
        mxx = np.maximum(self._off.index.max(), self._on.index.max()) + 1
        counts = pd.DataFrame({"count": mxx * np.ones(len(self._edge_indices), dtype=int),
                               "data": np.ones(len(self._edge_indices), dtype=bool)})
        off_times = self._off.reset_index().groupby("edge").apply(lambda x: np.hstack([x["t"].values, mxx]))
        on_times = self._on.reset_index().groupby("edge").apply(lambda x: np.hstack([0, x["t"].values]))

        def counter(arg):
            if not isinstance(arg["ton"], np.ndarray): return arg["toff"][0]
            ton = arg["ton"]
            return (arg["toff"][:len(ton)] - ton).sum()
        check = pd.concat([off_times, on_times], axis=1, keys=["toff", "ton"])
        check = check.apply(counter, axis=1)
        counts.loc[check.index, "count"] = check
        return ConnectivityMatrix(self._edge_indices, vertex_properties=self._vertex_properties,
                                  edge_properties=counts, default_edge_property="count")
    
    def is_consistent(self):
        from scipy.spatial import distance
        def simple_diff(a, b):
            return (b - a)[0]

        def valid(arg):
            if not isinstance(arg["toff"], np.ndarray): return False
            if not isinstance(arg["ton"], np.ndarray): return len(arg["toff"]) == 1
            mat = distance.cdist(arg["ton"], arg["toff"], metric=simple_diff)
            return ~np.any(mat == 0) and\
                np.all(np.triu(mat, 1) >= 0) and\
                np.all(np.tril(mat) <= 0)
        
        off_times = self._off.reset_index().groupby("edge").apply(lambda x: np.vstack(x["t"]))
        on_times = self._on.reset_index().groupby("edge").apply(lambda x: np.vstack(x["t"]))
        check = pd.concat([off_times, on_times], axis=1, keys=["toff", "ton"])
        check = check.apply(valid, axis=1)
        return check

    def fix_consistency(self, copy=False):
        is_on = set(range(len(self._edge_indices)))
        valid_on = pd.Series(np.ones(len(self._on), dtype=bool), index=self._on.index)
        valid_off = pd.Series(np.ones(len(self._off), dtype=bool), index=self._off.index)

        N = np.maximum(self._off.index.max(), self._on.index.max())

        for i in range(N + 1):
            if i in self._off.index:
                valid_off[i] = np.isin(self._off[i], list(is_on))
                is_on.difference_update(self._off[i])
            if i in self._on.index:
                valid_on[i] = ~np.isin(self._on[i], list(is_on))
                is_on.update(self._on[i])
        
        if copy:
            return StructurallyPlasticMatrix(self._edge_indices, vertex_properties=self._vertex_properties,
                                             edge_properties=self._edges, default_edge_property=self._default_edge,
                                             shape=self._shape,
                                             edge_off=self._off[valid_off],
                                             edge_on=self._on[valid_on], check_consistency=False)
        
        self._on = self._on[valid_on]; self._off = self._off[valid_off]
        return self
    
    @classmethod
    def from_matrix_stack(cls, mats, vertex_labels=None, vertex_properties=None,
                          default_edge_property="data"):
        assert len(mats) > 0
        ms = [sparse.coo_matrix(_m) for _m in mats]
        assert np.all([_m.shape == ms[0].shape for _m in ms])
        shape = ms[0].shape
        
        def mat2df(mat):
            return pd.DataFrame({
                "row": mat.row, "col": mat.col
            })
        dfs = list(map(mat2df, ms))

        df = pd.concat(dfs, axis=0).drop_duplicates().reset_index(drop=True)
        curr_idx = pd.MultiIndex.from_frame(df)

        off_dict = {}; on_dict = {}; tent_off = []
        for t in range(len(dfs)):
            new_idx = pd.MultiIndex.from_frame(dfs[t])
            tent_on = tent_off
            tent_off = np.nonzero([_idx not in new_idx for _idx in curr_idx])[0]
            on_dict[t] = np.setdiff1d(tent_on, tent_off)
            off_dict[t] = np.setdiff1d(tent_off, tent_on)
        
        edge_properties = pd.DataFrame({default_edge_property: np.ones(len(df), dtype=bool)})
        return cls(df, vertex_labels=vertex_labels, vertex_properties=vertex_properties,
                   edge_properties=edge_properties, default_edge_property=default_edge_property,
                   shape=shape, edge_off=off_dict, edge_on=on_dict)


class TimeDependentMatrix(ConnectivityMatrix):
    """Utility class to get, save, load and hold a time dependent weighted connections matrices"""
    def __init__(self, *args, vertex_labels=None, vertex_properties=None,
                 edge_properties=None, default_edge_property=None, shape=None):
        """Not too intuitive init - please see `from_report()` below"""
        if len(args) == 1 and isinstance(args[0], np.ndarray) or isinstance(args[0], sparse.spmatrix):
            raise ValueError("TimeDependentMatrix can only be initialized by edge indices and edge properties")
        if isinstance(edge_properties, dict):
            assert np.all([isinstance(x.columns, pd.Float64Index) for x in edge_properties.values()]),\
                 "Index of edge properties must be a Float64Index"
            edge_properties = pd.concat(edge_properties.values(), keys=edge_properties.keys(), names=["name"], axis=1)
            edge_properties.columns = edge_properties.columns.reorder_levels([1, 0])
        else:
            assert isinstance(edge_properties, pd.DataFrame)
            if isinstance(edge_properties.columns, pd.MultiIndex):
                assert len(edge_properties.columns.levels) == 2, "Columns must index time and name"
                if not isinstance(edge_properties.columns.levels[0], pd.Float64Index):
                    assert isinstance(edge_properties.columns.levels[1], pd.Float64Index),\
                        "Time index must be of type Float64Index"
                    edge_properties.columns = edge_properties.columns.reorder_levels([1, 0])
                else:
                    assert isinstance(edge_properties.columns.levels[0], pd.Float64Index),\
                        "Time index must be of type Float64Index"
            else:
                assert isinstance(edge_properties.columns, pd.Float64Index),\
                        "Time index must be of type Float64Index"
                edge_properties = pd.concat([edge_properties], axis=1, copy=False, keys=["data"], names=["name"])
                edge_properties.columns = edge_properties.columns.reorder_levels([1, 0])
        if default_edge_property is None:
            default_edge_property = edge_properties.columns.levels[1][0]
        self._time = edge_properties.columns.levels[0].min()
        super().__init__(*args, vertex_labels=vertex_labels, vertex_properties=vertex_properties,
                         edge_properties=edge_properties, default_edge_property=default_edge_property, shape=shape)
    
    @property
    def edges(self):
        return self._edges[self._time]
    
    def at_time(self, new_time):
        # TODO: Add a copy=True kwarg that acts like in .default
        if new_time not in self._edges:
            raise ValueError("No time point at {0} given".format(new_time))  # TODO: interpolate to nearest point?
        self._time = new_time
        return self
    
    def add_edge_property(self, new_label, new_values):
        raise NotImplementedError("Not yet implemented for TimeDependentMatrix")
    
    def default(self, new_default_property, copy=True):
        ret = super().default(new_default_property, copy=copy)
        if copy: ret._time = self._time
        return ret
    
    @classmethod
    def from_report(cls, sim, report_cfg, load_cfg=None, presyn_mapping=None):
        """
        A sonata synapse (compartment) report based constructor
        :param sim: bluepy Simulation object
        :param report_cfg: config dict with report's name, time steps to load,
                           static property name to look up for synapses that aren't reported,
                           and optionally the names of the aggregation functions to use
        :param load_cfg: config dict for loading and filtering neurons from the circuit
        :param presyn_mapping: mapping used to convert report from Neurodamus' post_gid & local_syn_id to
                               pre_gid and post_gid which can then be grouped and aggregated to get weighted connectomes
                               can be: pd.DataFrame with pre & post_gids and Neurodamus' local_syn_idx or
                                       filename of a saved DataFrame like that (loaded with `pd.read_pickle()`) or
                                       None (default) in which case the mapping will be calculated on the fly
        :return: a TimeDependentMatrix object
        """
        from .io.synapse_report import sonata_report, load_report, get_presyn_mapping, reindex_report, aggregate_data
        from .circuit_models.neuron_groups.grouping_config import load_filter

        load_config = _update_load_config(load_cfg, sim.config.Run["CircuitTarget"])
        nrn = load_filter(sim.circuit, load_config)
        lo_gids = pd.Series(range(len(nrn["gid"])), index=nrn["gid"])
        # TODO: What if the report is not on the local connectome?

        report, report_gids = sonata_report(sim, report_cfg)
        tgt_report_gids = np.intersect1d(nrn["gid"], report_gids)
        non_report_gids = np.setdiff1d(nrn["gid"], tgt_report_gids)
        data = load_report(report, report_cfg, tgt_report_gids)  # load only target post_gids

        if presyn_mapping is None or len(tgt_report_gids) < len(report_gids):
            # the saved mapping is defined based on the full report, so if parts are loaded one would need to filter
            # the mapping as well at which point, it's faster to just recalculate the whole thing
            presyn_mapping = get_presyn_mapping(sim.circuit.config["connectome"], data.index)
        if not isinstance(presyn_mapping, pd.DataFrame):
            presyn_mapping = pd.read_pickle(presyn_mapping)

        data = reindex_report(data, presyn_mapping)
        data = data.iloc[data.index.get_level_values(0).isin(nrn["gid"])]  # filter to have only target pre_gids
        print("Report read! Starting aggregation of {0} data points...".format(data.shape))

        edges = aggregate_data(data, report_cfg, lo_gids)

        if len(non_report_gids) > 0:
            from .circuit_models import circuit_connection_matrix
            print("Looking up static values for non-reported postsynaptic neurons...")
            agg_funcs = list(edges.columns.levels[0])
            time_stamps = edges.columns.levels[1]
            lo_nr_gids = pd.Series(non_report_gids)
            Ms = circuit_connection_matrix(sim.circuit, for_gids=nrn["gid"], for_gids_post=non_report_gids,
                                           edge_property=report_cfg["static_prop_name"], agg_func=agg_funcs)
            stat_edges = [pd.DataFrame.from_dict({t: Ms[agg_func].tocoo().data for t in time_stamps})
                          for agg_func in agg_funcs]
            stat_edges = pd.concat(stat_edges, axis=1, copy=False,  keys=agg_funcs)
            stat_edges.columns.set_names(edges.columns.names, inplace=True)
            # map (non-reported, local) col idx to gids and then back to (global) col idx
            stat_col_idx = lo_gids[lo_nr_gids[Ms[agg_funcs[0]].tocoo().col]].to_numpy()
            stat_edges.index = pd.MultiIndex.from_arrays(np.array([Ms[agg_funcs[0]].tocoo().row, stat_col_idx]),
                                                         names=["row", "col"])
            edges = edges.append(stat_edges)
            edges.sort_index(inplace=True)

        new_idxx = pd.RangeIndex(len(edges))
        edge_ids = edges.index.to_frame().set_index(new_idxx)
        edges = edges.set_index(new_idxx)
        shape = (len(nrn), len(nrn))
        return cls(edge_ids, edge_properties=edges, vertex_properties=nrn.set_index("gid"), shape=shape)


class ConnectivityInSubgroups(ConnectivityMatrix):

    def __extract_vertex_ids__(self, an_obj):
        if isinstance(an_obj, str):
            assert self._vertex_properties[an_obj].dtype == bool, "Population spec must be a column of type bool"
            return self.gids[self._vertex_properties[an_obj]]

        if hasattr(an_obj, GID):
            return getattr(an_obj, GID)
        return an_obj


class ConnectivityGroup(object):
    def __init__(self, *args):
        if len(args) == 1:
            assert isinstance(args[0].index, pd.MultiIndex)
            self._mats = args[0]
        elif len(args) == 2:
            self._mats = pd.Series(args[1], index=pd.MultiIndex.from_frame(args[0]))
        self._vertex_properties = pd.concat([x._vertex_properties for x in self._mats],
                                            copy=False, axis=0).drop_duplicates()
        
        for colname in self._vertex_properties.columns:
            #  TODO: Check colname against existing properties
            setattr(self, colname, self._vertex_properties[colname].values)

        # TODO: calling it "gids" might be too BlueBrain-specific! Change name?
        self.gids = self._vertex_properties.index.values
    
    @property
    def index(self):
        return self._mats.index

    @staticmethod
    def __loaditem__(args):
        return ConnectivityMatrix.from_h5(*args)
    
    def __load_if_needed__(self, args):
        if isinstance(args, ConnectivityMatrix) or isinstance(args, pd.Series):
            return args
        return self.__loaditem__(args)
    
    def __getitem__(self, key):
        return self.__load_if_needed__(self._mats[key])
    
    @classmethod
    def from_bluepy(cls, bluepy_obj, load_config=None, connectome=LOCAL_CONNECTOME, **kwargs):
        """
        BlueConfig/CircuitConfig based constructor
        :param bluepy_obj: bluepy Simulation or Circuit object
        :param load_config: config dict for loading and filtering neurons from the circuit
        :param connectome: str. that can be "local" which specifies local circuit connectome
                           or the name of a projection to use
        Additional **kwargs are forwarded to a call of conntility.circuit_models.circuit_group_matrices
        """
        from .circuit_models.neuron_groups import load_group_filter
        from .circuit_models import circuit_group_matrices

        if hasattr(bluepy_obj, "circuit"):
            circ = bluepy_obj.circuit
        else:
            circ = bluepy_obj
        
        nrn = load_group_filter(circ, load_config)

        # TODO: think a bit about if it should even be possible to call this for a projection (remove arg. if not...)
        mats = circuit_group_matrices(circ, nrn, connectome=connectome, **kwargs)
        nrns = [nrn.loc[x].set_index(GID) for x in mats.keys()]
        con_obj = [ConnectivityMatrix(mat, vertex_properties=n) for n, mat in zip(nrns, mats)]
        return cls(pd.Series(con_obj, index=mats.index))
    
    @classmethod
    def from_h5(cls, fn, group_name=None, prefix=None):
        raise NotImplementedError()

    def to_h5(self, fn, group_name=None, prefix=None):
        if prefix is None:
            prefix = "connectivity"
        if group_name is None:
            group_name = "conn_group"
        full_prefix = prefix + "/" + group_name
        self._vertex_properties.to_hdf(fn, key=full_prefix + "/vertex_properties", format="table")

        matrix_prefix = full_prefix + "/matrices"

        def _store(mat):
            global _MAT_GLOBAL_INDEX
            grp_name = "matrix{0}".format(_MAT_GLOBAL_INDEX)
            mat.to_h5(fn, group_name=grp_name, prefix=matrix_prefix)
            _MAT_GLOBAL_INDEX = _MAT_GLOBAL_INDEX + 1
            return "::".join([fn, matrix_prefix, grp_name])

        mats = self._mats.apply(_store)
        mats.reset_index().to_hdf(fn, key=full_prefix + "/table", format="table")

        with h5py.File(fn, "a") as h5:
            data_grp = h5[full_prefix]
            data_grp.attrs["NEUROTOP_CLASS"] = "ConnectivityGroup"

