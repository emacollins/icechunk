#!/usr/bin/env python3
from collections.abc import Hashable, Mapping, MutableMapping
from typing import Any, Literal, Union

import zarr

from icechunk import IcechunkStore
from .dask import stateful_store_reduce

from xarray import Dataset
from xarray.core.types import ZarrWriteModes  # TODO: delete this
from xarray.backends.zarr import ZarrStore
from xarray.backends.common import ArrayWriter
from dataclasses import dataclass, field

# TODO: import-time check on Xarray version
#
try:
    import dask

    has_dask = True
except ImportError:
    has_dask = False


def extract_stores(zarray: zarr.Array) -> IcechunkStore:
    return zarray.store


def merge_stores(*stores: IcechunkStore) -> IcechunkStore:
    store, *rest = stores
    for other in rest:
        store.merge(other.change_set_bytes())
    return store


def is_chunked_array(x: Any) -> bool:
    if has_dask:
        import dask

        return dask.base.is_dask_collection(x)
    else:
        return False


class LazyArrayWriter(ArrayWriter):
    def __init__(self) -> None:
        super().__init__()

        self.eager_sources = []
        self.eager_targets = []
        self.eager_regions = []

    def add(self, source, target, region=None):
        if is_chunked_array(source):
            self.sources.append(source)
            self.targets.append(target)
            self.regions.append(region)
        else:
            self.eager_sources.append(source)
            self.eager_targets.append(target)
            self.eager_regions.append(region)

    def write_eager(self) -> None:
        for source, target, region in zip(
            self.eager_sources, self.eager_targets, self.eager_regions, strict=True
        ):
            target[region or ...] = source
        self.eager_sources = []
        self.eager_targets = []
        self.eager_regions = []


@dataclass
class XarrayDatasetWriter:
    """
    Write Xarray Datasets to a group in an Icechunk store.
    """

    dataset: Dataset = field(repr=False)
    store: IcechunkStore = field(kw_only=True)

    _initialized: bool = field(default=False, repr=False)

    xarray_store: ZarrStore = field(init=False, repr=False)
    writer: LazyArrayWriter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.store, IcechunkStore):
            raise ValueError(
                f"Please pass in an IcechunkStore. Recevied {type(self.store)!r} instead."
            )

    def write_metadata(
        self,
        *,
        group: str | None = None,
        mode: ZarrWriteModes | None = None,
        encoding: Mapping | None = None,
        append_dim: Hashable | None = None,
        region: Mapping[str, slice | Literal["auto"]] | Literal["auto"] | None = None,
        write_empty_chunks: bool | None = None,
        safe_chunks: bool = True,
    ) -> None:
        """
        This method creates new Zarr arrays when necessary, writes attributes,
        and any in-memory arrays.

        Parameters
        ----------
        group : str, optional
            Group path. (a.k.a. `path` in zarr terminology.)
        mode : {"w", "w-", "a", "a-", r+", None}, optional
            Persistence mode: "w" means create (overwrite if exists);
            "w-" means create (fail if exists);
            "a" means override all existing variables including dimension coordinates (create if does not exist);
            "a-" means only append those variables that have ``append_dim``.
            "r+" means modify existing array *values* only (raise an error if
            any metadata or shapes would change).
            The default mode is "a" if ``append_dim`` is set. Otherwise, it is
            "r+" if ``region`` is set and ``w-`` otherwise.
        encoding : dict, optional
            Nested dictionary with variable names as keys and dictionaries of
            variable specific encodings as values, e.g.,
            ``{"my_variable": {"dtype": "int16", "scale_factor": 0.1,}, ...}``
        append_dim : hashable, optional
            If set, the dimension along which the data will be appended. All
            other dimensions on overridden variables must remain the same size.
        region : dict or "auto", optional
            Optional mapping from dimension names to either a) ``"auto"``, or b) integer
            slices, indicating the region of existing zarr array(s) in which to write
            this dataset's data.

            If ``"auto"`` is provided the existing store will be opened and the region
            inferred by matching indexes. ``"auto"`` can be used as a single string,
            which will automatically infer the region for all dimensions, or as
            dictionary values for specific dimensions mixed together with explicit
            slices for other dimensions.

            Alternatively integer slices can be provided; for example, ``{'x': slice(0,
            1000), 'y': slice(10000, 11000)}`` would indicate that values should be
            written to the region ``0:1000`` along ``x`` and ``10000:11000`` along
            ``y``.

            Users are expected to ensure that the specified region aligns with
            Zarr chunk boundaries, and that dask chunks are also aligned.
            Xarray makes limited checks that these multiple chunk boundaries line up.
            It is possible to write incomplete chunks and corrupt the data with this
            option if you are not careful.
        safe_chunks : bool, default: True
            If True, only allow writes to when there is a many-to-one relationship
            between Zarr chunks (specified in encoding) and Dask chunks.
            Set False to override this restriction; however, data may become corrupted
            if Zarr arrays are written in parallel.
            In addition to the many-to-one relationship validation, it also detects partial
            chunks writes when using the ``region`` parameter,
            these partial chunks are considered unsafe in the mode "r+" but safe in
            the mode "a".
            Note: Even with these validations it can still be unsafe to write
            two or more chunked arrays in the same location in parallel if they are
            not writing in independent regions.
        write_empty_chunks : bool or None, optional
            If True, all chunks will be stored regardless of their
            contents. If False, each chunk is compared to the array's fill value
            prior to storing. If a chunk is uniformly equal to the fill value, then
            that chunk is not be stored, and the store entry for that chunk's key
            is deleted. This setting enables sparser storage, as only chunks with
            non-fill-value data are stored, at the expense of overhead associated
            with checking the data of each chunk. If None (default) fall back to
            specification(s) in ``encoding`` or Zarr defaults. A ``ValueError``
            will be raised if the value of this (if not None) differs with
            ``encoding``.

        Returns
        -------
        None

        Notes
        -----
        Two restrictions apply to the use of ``region``:

          - If ``region`` is set, _all_ variables in a dataset must have at
            least one dimension in common with the region. Other variables
            should be written in a separate single call to ``to_zarr()``.
          - Dimensions cannot be included in both ``region`` and
            ``append_dim`` at the same time. To create empty arrays to fill
            in with ``region``, use the `XarrayDatasetWriter` directly.
        """
        from xarray.backends.zarr import _choose_default_mode
        from xarray.backends.api import _validate_dataset_names, dump_to_store

        # validate Dataset keys, DataArray names
        _validate_dataset_names(self.dataset)

        self.mode = _choose_default_mode(mode=mode, append_dim=append_dim, region=region)

        self.xarray_store = ZarrStore.open_group(
            store=self.store,
            group=group,
            mode=mode,
            zarr_format=3,
            append_dim=append_dim,
            write_region=region,
            safe_chunks=safe_chunks,
            write_empty=write_empty_chunks,
            synchronizer=None,
            consolidated=False,
            consolidate_on_close=False,
            zarr_version=None,
        )

        if encoding is None:
            encoding = {}
        self.xarray_store._validate_encoding(encoding)

        dataset = self.xarray_store._validate_and_autodetect_region(self.dataset)

        # This writes the metadata (zarr.json) for all arrays
        self.writer = LazyArrayWriter()
        dump_to_store(dataset, self.xarray_store, self.writer, encoding=encoding)

        self._initialized = True

    def write_eager(self):
        """
        Write in-memory variables to store.

        Returns
        -------
        None
        """
        if not self._initialized:
            raise ValueError("Please call `write_metadata` first.")
        self.writer.write_eager()

    def write_lazy(
        self,
        chunkmanager_store_kwargs: MutableMapping | None = None,
        split_every: int | None = None,
    ) -> None:
        """
        Write lazy arrays (e.g. dask) to store.
        """
        if not self._initialized:
            raise ValueError("Please call `write_metadata` first.")

        if not self.writer.sources:
            return

        chunkmanager_store_kwargs = chunkmanager_store_kwargs or {}
        chunkmanager_store_kwargs["load_stored"] = False
        chunkmanager_store_kwargs["return_stored"] = True

        # This calls dask.array.store, and we receive a dask array where each chunk is a Zarr array
        # each of those zarr.Array.store contains the changesets we need
        stored_arrays = self.writer.sync(
            compute=False, chunkmanager_store_kwargs=chunkmanager_store_kwargs
        )

        # Now we tree-reduce all changesets
        merged_store = stateful_store_reduce(
            stored_arrays,
            prefix="ice-changeset",
            chunk=extract_stores,
            aggregate=merge_stores,
            split_every=split_every,
            compute=True,
            **chunkmanager_store_kwargs,
        )
        self.store.merge(merged_store.change_set_bytes())


def to_icechunk(
    dataset: Dataset,
    store: IcechunkStore,
    *,
    group: str | None = None,
    mode: ZarrWriteModes | None = None,
    write_empty_chunks: bool | None = None,
    safe_chunks: bool = True,
    append_dim: Hashable | None = None,
    region: Mapping[str, slice | Literal["auto"]] | Literal["auto"] | None = None,
    encoding: Mapping | None = None,
    chunkmanager_store_kwargs: MutableMapping | None = None,
    split_every: int | None = None,
    **kwargs,
) -> None:
    """
    Write an Xarray Dataset to a group of an icechunk store.

    Parameters
    ----------
    store : MutableMapping, str or path-like, optional
        Store or path to directory in local or remote file system.
    mode : {"w", "w-", "a", "a-", r+", None}, optional
        Persistence mode: "w" means create (overwrite if exists);
        "w-" means create (fail if exists);
        "a" means override all existing variables including dimension coordinates (create if does not exist);
        "a-" means only append those variables that have ``append_dim``.
        "r+" means modify existing array *values* only (raise an error if
        any metadata or shapes would change).
        The default mode is "a" if ``append_dim`` is set. Otherwise, it is
        "r+" if ``region`` is set and ``w-`` otherwise.
    group : str, optional
        Group path. (a.k.a. `path` in zarr terminology.)
    encoding : dict, optional
        Nested dictionary with variable names as keys and dictionaries of
        variable specific encodings as values, e.g.,
        ``{"my_variable": {"dtype": "int16", "scale_factor": 0.1,}, ...}``
    append_dim : hashable, optional
        If set, the dimension along which the data will be appended. All
        other dimensions on overridden variables must remain the same size.
    region : dict or "auto", optional
        Optional mapping from dimension names to either a) ``"auto"``, or b) integer
        slices, indicating the region of existing zarr array(s) in which to write
        this dataset's data.

        If ``"auto"`` is provided the existing store will be opened and the region
        inferred by matching indexes. ``"auto"`` can be used as a single string,
        which will automatically infer the region for all dimensions, or as
        dictionary values for specific dimensions mixed together with explicit
        slices for other dimensions.

        Alternatively integer slices can be provided; for example, ``{'x': slice(0,
        1000), 'y': slice(10000, 11000)}`` would indicate that values should be
        written to the region ``0:1000`` along ``x`` and ``10000:11000`` along
        ``y``.

        Users are expected to ensure that the specified region aligns with
        Zarr chunk boundaries, and that dask chunks are also aligned.
        Xarray makes limited checks that these multiple chunk boundaries line up.
        It is possible to write incomplete chunks and corrupt the data with this
        option if you are not careful.
    safe_chunks : bool, default: True
        If True, only allow writes to when there is a many-to-one relationship
        between Zarr chunks (specified in encoding) and Dask chunks.
        Set False to override this restriction; however, data may become corrupted
        if Zarr arrays are written in parallel.
        In addition to the many-to-one relationship validation, it also detects partial
        chunks writes when using the region parameter,
        these partial chunks are considered unsafe in the mode "r+" but safe in
        the mode "a".
        Note: Even with these validations it can still be unsafe to write
        two or more chunked arrays in the same location in parallel if they are
        not writing in independent regions.
    write_empty_chunks : bool or None, optional
        If True, all chunks will be stored regardless of their
        contents. If False, each chunk is compared to the array's fill value
        prior to storing. If a chunk is uniformly equal to the fill value, then
        that chunk is not be stored, and the store entry for that chunk's key
        is deleted. This setting enables sparser storage, as only chunks with
        non-fill-value data are stored, at the expense of overhead associated
        with checking the data of each chunk. If None (default) fall back to
        specification(s) in ``encoding`` or Zarr defaults. A ``ValueError``
        will be raised if the value of this (if not None) differs with
        ``encoding``.
    chunkmanager_store_kwargs : dict, optional
        Additional keyword arguments passed on to the `ChunkManager.store` method used to store
        chunked arrays. For example for a dask array additional kwargs will be passed eventually to
        `dask.array.store()`. Experimental API that should not be relied upon.

    Returns
    -------
    None

    Notes
    -----
    Two restrictions apply to the use of ``region``:

      - If ``region`` is set, _all_ variables in a dataset must have at
        least one dimension in common with the region. Other variables
        should be written in a separate single call to ``to_zarr()``.
      - Dimensions cannot be included in both ``region`` and
        ``append_dim`` at the same time. To create empty arrays to fill
        in with ``region``, use the `XarrayDatasetWriter` directly.
    """
    writer = XarrayDatasetWriter(dataset, store=store)
    # write metadata
    writer.write_metadata(
        group=group,
        mode=mode,
        encoding=encoding,
        append_dim=append_dim,
        region=region,
        write_empty_chunks=write_empty_chunks,
        safe_chunks=safe_chunks,
    )
    # write in-memory arrays
    writer.write_eager()
    # eagerly write dask arrays
    writer.write_lazy(chunkmanager_store_kwargs=chunkmanager_store_kwargs)
