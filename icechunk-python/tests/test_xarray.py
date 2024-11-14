import pytest

pytest.importorskip("xarray")

import xarray as xr
import numpy as np
import string
import pandas as pd
from xarray.testing import assert_identical
from icechunk.xarray import to_icechunk
import contextlib
import tempfile
from icechunk import IcechunkStore, StorageConfig


def create_test_data(
    seed: int | None = None,
    add_attrs: bool = True,
    dim_sizes: tuple[int, int, int] = (8, 9, 10),
) -> xr.Dataset:
    rs = np.random.RandomState(seed)
    _vars = {
        "var1": ["dim1", "dim2"],
        "var2": ["dim1", "dim2"],
        "var3": ["dim3", "dim1"],
    }
    _dims = {"dim1": dim_sizes[0], "dim2": dim_sizes[1], "dim3": dim_sizes[2]}

    obj = xr.Dataset()
    obj["dim2"] = ("dim2", 0.5 * np.arange(_dims["dim2"]))
    if _dims["dim3"] > 26:
        raise RuntimeError(
            f'Not enough letters for filling this dimension size ({_dims["dim3"]})'
        )
    obj["dim3"] = ("dim3", list(string.ascii_lowercase[0 : _dims["dim3"]]))
    obj["time"] = ("time", pd.date_range("2000-01-01", periods=20))
    for v, dims in sorted(_vars.items()):
        data = rs.normal(size=tuple(_dims[d] for d in dims))
        obj[v] = (dims, data)
        if add_attrs:
            obj[v].attrs = {"foo": "variable"}
    numbers_values = rs.randint(0, 3, _dims["dim3"], dtype="int64")
    obj.coords["numbers"] = ("dim3", numbers_values)
    obj.encoding = {"foo": "bar"}
    return obj


@contextlib.contextmanager
def roundtrip(data: xr.Dataset):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = IcechunkStore.create(StorageConfig.filesystem(tmpdir))
        to_icechunk(data, store=store, mode="w")
        with xr.open_zarr(store, consolidated=False) as ds:
            yield ds


def test_xarray_to_icechunk():
    ds = create_test_data()
    with roundtrip(ds) as actual:
        assert_identical(actual, ds)
