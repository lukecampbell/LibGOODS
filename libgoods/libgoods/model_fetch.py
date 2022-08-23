#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A module containing code for fetching content from models."""
import time
from typing import List, Tuple, Mapping, Optional
from pathlib import Path
from datetime import datetime
from dataclasses import field, dataclass

import pandas as pd
import xarray as xr
import requests
import model_catalogs as mc
from extract_model import utils as em_utils

DEFAULT_STANDARD_NAMES = [
    "eastward_sea_water_velocity",
    "northward_sea_water_velocity",
    "eastward_wind",
    "northward_wind",
    "sea_water_temperature",
    "sea_water_practical_salinity",
    "sea_floor_depth",
]


class Timer:
    """A class to aid with measuring timing."""

    def __init__(self, msg=None):
        """Initializes the current time."""
        self.t0 = time.time()
        self.msg = msg

    def tick(self):
        """Update the timer start."""
        self.t0 = time.time()

    def tock(self) -> float:
        """Return elapsed time in ms."""
        return (time.time() - self.t0) * 1000.0

    def format(self):
        time_in_ms = self.tock()
        if time_in_ms > 60000:
            return f"{time_in_ms / 60000:.1f} min"
        if time_in_ms > 2000:
            return f"{time_in_ms / 1000:.1f} s"
        return f"{time_in_ms:.1f} ms"

    def __enter__(self):
        """With context."""
        self.tick()

    def __exit__(self, type, value, traceback):
        """With exit."""
        if self.msg is not None:
            print(self.msg.format(self.format()))


@dataclass
class FetchConfig:
    """Configuration data class for fetching."""

    model_name: str
    output_pth: Path
    start: pd.Timestamp
    end: pd.Timestamp
    bbox: Optional[Tuple[float, float, float, float]]
    timing: str
    standard_names: List[str] = field(default_factory=lambda: DEFAULT_STANDARD_NAMES)
    surface_only: bool = False


def _select_surface(ds: xr.Dataset) -> xr.Dataset:
    """Return a dataset that is reduced to only the surface layer."""
    model_guess = em_utils.guess_model_type(ds)
    if all([ds[zaxis].ndim < 2 for zaxis in ds.cf.axes["Z"]]):
        return ds.cf.sel(Z=0, method="nearest")
    elif model_guess == "FVCOM":
        vertical_dims = set()
        for varname in ds.cf.axes["Z"]:
            vertical_dim = ds[varname].dims[0]
            vertical_dims.add(vertical_dim)
        sel_kwargs = {vdim: 0 for vdim in vertical_dims}
        return ds.isel(**sel_kwargs)
    elif model_guess == "SELFE":
        return ds.isel(nv=-1)
    raise ValueError("Can't decode vertical coordinates.")


def get_times(model_name: str) -> Mapping[str, pd.Timestamp]:
    """Return a mapping of a source to the start and end datetimes."""
    main_cat = mc.setup()
    cat = mc.find_availability(main_cat[model_name])
    timing_date_map = {}
    for timing in cat:
        timing_date_map[timing] = (
            pd.Timestamp(cat[timing].metadata["start_datetime"]),
            pd.Timestamp(cat[timing].metadata["end_datetime"]),
        )
    return timing_date_map


def get_source_online_status(model_name: str) -> Mapping[str, bool]:
    """Return a mapping of source to a boolean indicating if the source is available."""
    yesterday = pd.Timestamp.today() - pd.Timedelta('1 day')
    main_cat = mc.setup()
    statuses = {}
    for timing in main_cat[model_name]:
        main_cat[model_name][timing]._pick()
        urlpath = main_cat[model_name][timing]._source(yesterday=yesterday).urlpath
        if isinstance(urlpath, list):
            urlpath = urlpath[0]
        resp = requests.get(urlpath + ".das")
        if resp.status_code != 200:
            statuses[timing] = False
        else:
            statuses[timing] = True
    return statuses


def get_bounds(model_name: str) -> Tuple[float, float, float, float]:
    """Returns the geospatial extents for the model."""
    main_cat = mc.setup()
    return main_cat[model_name].metadata["bounding_box"]


def fetch(fetch_config: FetchConfig):
    """Downloads and subsets the model data.

    Parameters
    ----------
    fetch_config : FetchConfig
        The configuration object which contains the model name, timing, start/end dates of the
        request, etc.
    """
    print("Setting up source catalog")
    with Timer("\tSource catalog generated in {}"):
        main_cat = mc.setup()

    print(
        f"Generating catalog specific for {fetch_config.model_name} {fetch_config.timing}"
    )
    with Timer("\tSpecific catalog generated in {}"):
        source = mc.select_date_range(
            main_cat[fetch_config.model_name],
            start_date=fetch_config.start,
            end_date=fetch_config.end,
            timing=fetch_config.timing,
        )

    print("Getting xarray dataset for model data")
    with Timer("\tCreated dask-based xarray dataset in {}"):
        ds = source.to_dask()

    if fetch_config.surface_only:
        print("Selecting only surface data.")
        with Timer("\tIndexed surface data in {}"):
            ds = _select_surface(ds)

    print("Subsetting data")
    with Timer("\tSubsetted dataset in {}"):
        ds_ss = ds.em.filter(fetch_config.standard_names)
        if fetch_config.bbox is not None:
            ds_ss = ds_ss.em.sub_grid(bbox=fetch_config.bbox)
    # print("Loading dataset into memory.")
    # with Timer("\tLoaded into memory in {}"):
    #    #ds_ss = ds_ss.load(scheduler="processes")
    #    ds_ss = ds_ss.load()
    print(
        f"Writing netCDF data to {fetch_config.output_pth}. This may take a long time..."
    )
    with Timer("\tWrote output to disk in {}"):
        ds_ss.to_netcdf(fetch_config.output_pth)
    print("Complete")