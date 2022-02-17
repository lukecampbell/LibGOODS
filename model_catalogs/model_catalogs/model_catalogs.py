"""
Everything dealing with the catalogs.
"""

import cf_xarray
import fnmatch
import intake
import numpy as np
import os
import pandas as pd
import shapely.geometry

# import xarray as xr

from intake.catalog import Catalog
from intake.catalog.local import LocalCatalogEntry
from siphon.catalog import TDSCatalog
import intake.source.derived


from glob import glob


class DatasetFixes(intake.source.derived.DerivedSource):

    def fix_calendar(self, ds):
        ds.time.calendar = "proleptic_gregorian"
        return xr.decode_cf(ds)


def find_bbox(ds, dd=None, alpha=None):
    """Determine bounds and boundary of model.

    Parameters
    ----------
    ds: Dataset
        xarray Dataset containing model output.
    dd: int, optional
        Number to decimate model output lon/lat by, as a stride.
    alpha: float, optional
        Number for alphashape to determine what counts as the convex hull.
        Larger number is more detailed, 1 is a good starting point.

    Returns
    -------
    List containing the name of the longitude and latitude variables for ds, geographic bounding box of model output: [min_lon, min_lat, max_lon, max_lat], low res and high res wkt representation of model boundary.
    """

    try:
        lon = ds.cf["longitude"].values
        lat = ds.cf["latitude"].values
        lonkey = ds.cf["longitude"].name
        latkey = ds.cf["latitude"].name

    except KeyError as e:
        lonkey = list(ds.cf[["longitude"]].coords.keys())[0]
        # need to make sure latkey matches lonkey grid
        latkey = f'lat{lonkey[3:]}'
        # latkey = list(ds.cf[["latitude"]].coords.keys())[0]
        # In case there are multiple grids, just take first one;
        # they are close enough
        lon = ds[lonkey].values
        lat = ds[latkey].values
    # import pdb; pdb.set_trace()
    if lon.ndim == 2:  # this is structured
        lonb = np.concatenate((lon[:, 0], lon[-1, :], lon[::-1, -1], lon[0, ::-1]))
        latb = np.concatenate((lat[:, 0], lat[-1, :], lat[::-1, -1], lat[0, ::-1]))
        boundary = np.vstack((lonb, latb)).T
        p = shapely.geometry.Polygon(zip(lonb, latb))
        p0 = p.simplify(1)
        p1 = p

    elif (lon.ndim == 1) and ('nele' not in ds.dims):  # This is structured

        nlon, nlat = ds["lon"].size, ds["lat"].size
        lonb = np.concatenate(([lon[0]] * nlat, lon[:], [lon[-1]] * nlat, lon[::-1]))
        latb = np.concatenate((lat[:], [lat[-1]] * nlon, lat[::-1], [lat[0]] * nlon))
        boundary = np.vstack((lonb, latb)).T
        p = shapely.geometry.Polygon(zip(lonb, latb))
        p0 = p.simplify(1)
        p1 = p

    elif (lon.ndim == 1) and ('nele' in ds.dims):  # unstructured

        assertion = 'dd and alpha need to be defined in the source_catalog for this model.'
        assert dd is not None and alpha is not None, assertion

        # need to calculate concave hull or alphashape of grid
        import alphashape

        # low res, same as convex hull
        p0 = alphashape.alphashape(list(zip(lon,lat)), 0.0)
        # downsample a bit to save time, still should clearly see shape of domain
        # dd = 10
        pts = shapely.geometry.MultiPoint(list(zip(lon[::dd],lat[::dd])))
        p1 = alphashape.alphashape(pts, alpha)
        # p1 = alphashape.alphashape(list(zip(lon,lat)), 10.)

    # useful things to look at: p.wkt  #shapely.geometry.mapping(p)
    return lonkey, latkey, list(p0.bounds), p0.wkt, p1.wkt


def make_catalog(cats, full_cat_name, full_cat_description, full_cat_metadata,
                 cat_driver, cat_path=None):
    """Construct single catalog from multiple catalogs or sources.

    Parameters
    ----------
    cats: list
       List of Intake catalog or source objects that will be combined into a single catalog.
    full_cat_name: str
       Name of overall catalog.
    full_cat_descrption: str
       Description of overall catalog.
    full_cat_metadata: dict
       Dictionary of metadata for overall catalog.
    cat_driver: str or Intake object
       Driver to apply to all catalog entries. For example:
       * intake.catalog.local.YAMLFileCatalog
       * 'opendap'
    cat_path: str, optional
       Path with catalog name to use for saving catalog. With or without yaml suffix. If not provided,
       will use `full_cat_name`.

    Returns
    -------

    Intake catalog.

    Examples
    --------

    Make catalog:

    >>> make_catalog([list of catalogs], 'catalog name', 'catalog desc', {}, 'opendap')
    """

    if cat_path is None:
        cat_path = full_cat_name
    if ('yaml' not in cat_path) and ('yml' not in cat_path):
        cat_path = f'{cat_path}.yaml'

    # create dictionary of catalog entries
    entries = {
               cat.name: LocalCatalogEntry(cat.name,
                                           description=cat.description,
                                           driver=cat_driver,
                                           args=cat._yaml()['sources'][cat.name]['args'],
                                           metadata=cat._yaml()['sources'][cat.name]['metadata']
                                           )
               for cat in cats
   }

    # create catalog
    cat = Catalog.from_dict(entries,
                            name=full_cat_name,
                            description=full_cat_description,
                            metadata=full_cat_metadata
    )

    # save catalog
    cat.save(cat_path)

    return cat


def agg_for_date(date, catloc, filetype, treat_last_day_as_forecast=False, pattern=None):
    """Aggregate NOAA OFS-style nowcast/forecast files.

    Parameters
    ----------
    date: str of datetime, pd.Timestamp
        Date of day to find model output files for. Doesn't pay attention to hours/minutes/seconds.
    catloc: str
        URL of thredds base catalog. Should be a pattern like the following in which the date will be
        filled in as a f-string variable. Can be xml or html:
        https://opendap.co-ops.nos.noaa.gov/thredds/catalog/NOAA/CBOFS/MODELS/{date.year}/{str(date.month).zfill(2)}/{str(date.day).zfill(2)}/catalog.xml
    filetype: str
        Which filetype to use. Every NOAA OFS model has "fields" available, but some have "regulargrid"
        or "2ds" also. This availability information is in the source catalog for the model under
        `filetypes` metadata.
    treat_last_day_as_forecast: bool, optional
        If True, then date is the last day of the time period being sought and the forecast files
        should be brought in along with the nowcast files, to get the model output the length of the
        forecast out in time. The forecast files brought in will have the latest timing cycle of the
        day that is available. If False, all nowcast files (for all timing cycles) are brought in.
    pattern: str, optional
        If a model file pattern doesn't match that assumed in this code, input one that will work.
        Currently only NYOFS doesn't match but the pattern is built into the catalog file.

    Returns
    -------
    List of URLs for where to find all of the model output files that match the keyword arguments.
    """

    if not isinstance(date, pd.Timestamp):
        date = pd.Timestamp(date)

    # f format catloc name with input date
    catloc = eval(f"f'{catloc}'")
    cat = TDSCatalog(catloc)

    # # brings in nowcast and forecast for any date in the catalog
    # pattern0 = f'*{filetype}*.t??z.*'
    # # brings in nowcast and forecast for only the day specified
    # pattern0 = date.strftime(f'*{filetype}*.%Y%m%d.t??z.*')

    if pattern is None:
        pattern = date.strftime(f'*{filetype}*.n*.%Y%m%d.t??z.*')
        # pattern = date.strftime(f'*{filetype}*.n???.%Y%m%d.t??z.*')
    else:
        pattern = eval(f"f'{pattern}'")
    # pattern = date.strftime(f'*{filetype}*.n*.%Y%m%d.t??z.*')

    # '*{filetype}.n???.{date.year}{str(date.month).zfill(2)}{str(date.day).zfill(2)}.t??z.*'

    # pattern = eval(f"f'{pattern}'")
    # import pdb; pdb.set_trace()
    fnames = fnmatch.filter(cat.datasets, pattern)

    if treat_last_day_as_forecast:

        import re
        regex = re.compile('.t[0-9]{2}z.')
        cycle = sorted(list(set([substr[2:4] for substr in regex.findall(''.join(cat.datasets))])))[-1]
        # cycle = sorted(list(set([fname[ fname.find(start:='.t') + len(start):fname.find('z.')] for fname in fnames])))[-1]
        # import pdb; pdb.set_trace()
        # pattern1 = f'*{filetype}*.t{cycle}z.*'

        # replace '.t??z.' in pattern with '.t{cycle}z.' and replace '.n*.' with '.*.'
        pattern1 = pattern.replace('.t??z.', '.t{cycle}z.').replace('.n*.', '.*.')
        pattern1 = eval(f"f'{pattern1}'")
        fnames = fnmatch.filter(cat.datasets, pattern1)

    filelocs = [cat.datasets.get(fname).access_urls['OPENDAP'] for fname in fnames]

    return filelocs


class Management():
    """Class to manage up to 3 different versions of model catalogs:

    * source: combined catalog for all available models from hard-wired info in source_catalogs
    * updated: optional combined catalog for all or subset of available models. Benefit of using
        this is that some specific metadata is read in from the forecast-version of the models
        themselves, which may be useful for deciding which model to use.
    * user: this catalog contains only the one or few specific model setups for a user project.


    Attributes
    ----------
    catalog_path: str
        Provide base location for catalogs. Default location is in the working directory,
        in a directory called "catalogs".
    cat_source_base: str
        Provide base location for source catalogs. Default location is in the working directory,
        in a directory called f'{self.catalog_path}/source_catalogs'.
    cat_updated_base: str
        Provide base location for updated catalogs. Default location is in the working directory,
        in a directory called f'{self.catalog_path}/updated_catalogs'.
    cat_user_base: str
        Provide base location for user catalogs. Default location is in the working directory,
        in a directory called f'{self.catalog_path}/user_catalogs'.
    time_ref: pd.Timestamp
        The time when the class is initialized. This will be used to "version" any updated and
        user catalogs that are created in this object.
    source_catalog_name: str
        Full relative path of the source catalog, as defined by
        f'{self.cat_source_base}/{source_catalog_name}'.
    source_catalog_dir: str
        Subdirectory in source_catalogs specifying which source catalogs have been used for models.
        Default location is in the working directory,
        in a directory called f'{self.cat_source_base}/orig' and taking the most recent one.
        Alternatively the user can input which date to use.
    source_cat: intake Catalog object
        Source catalog, containing references to all known models in the source_catalogs directory.
        This is all hard-wired information about the models.
    updated_catalog_dir: str
        Subdirectory in updated_catalogs specifying which updated catalogs have been used for models.
        Location is in the working directory,
        in a directory called f'{self.cat_updated_base}/{self.time_ref.isoformat()}'
    updated_catalog_name: str
        Full relative path of the updated catalog, as defined by
        f'{self.cat_updated_dir}/{updated_catalog_name}'.
    """

    def __init__(self, catalog_path='../catalogs',
                 source_catalog_name='source_catalog.yaml', make_source_catalog=False,
                 source_ref_date=None
                 ):
        """Initialize a Management class object.

        Parameters
        ----------
        catalog_path: str, optional
            Provide base location for catalogs. Default location is in the working directory,
            in a directory called "catalogs".
        source_catalog_name: str, optional
            Alternative name to use for source catalog. Default is 'source_catalog.yaml'.
            Suffix isn't necessary.
        make_source_catalog: boolean, optional
            The source catalog is not meant to change often. So, if source_catalog_name in
            catalog_path already exists and make_source_catalog is False, it will simply be
            read in. Otherwise, the source catalog will be recreated.
        source_ref_date: str, optional
            Reference date for directory to use to find source_catalog files. If None,
            code will choose most recent.

        Returns
        -------
        Management class object which is initialized with `.source_cat`.

        Examples
        --------
        >>> from goods import catalogs
        >>> cats = catalogs.Management()
        >>> cats.source_cat
        """

        self.catalog_path = catalog_path
        self.cat_source_base = f'{self.catalog_path}/source_catalogs'
        self.cat_updated_base = f'{self.catalog_path}/updated_catalogs'
        self.cat_user_base = f'{self.catalog_path}/user_catalogs'
        self.time_ref = pd.Timestamp.now()

        # find most recent set of source_catalogs
        if source_ref_date is None:
            which_dir = f'{self.cat_source_base}/orig'
            # if there are newer directories of source catalogs, use
            # the most recent one
            if len(glob(f'{self.cat_source_base}/????-??-??'))>0:
                which_dir = sorted(glob(f'{self.cat_source_base}/????-??-??'))[-1]
        else:
            which_dir = f'{self.cat_source_base}/{source_ref_date}'
        self.source_catalog_dir = which_dir

        # Read in already-available model source catalog
        self.source_catalog_name = f'{self.cat_source_base}/{source_catalog_name}'
        if os.path.exists(self.source_catalog_name) and not make_source_catalog:
            self.source_cat = intake.open_catalog(self.source_catalog_name)

        else:  # otherwise, make it
            self.setup_source_catalog()

        # initialize user catalogs
        # self.user_cats = []


    def setup_source_catalog(self):
        """Setup source catalog for models.

        Returns
        -------
        Nothing, but sets up `.source_cat`.
        """

        cat_source_description = 'Source catalog for models.'

        # open catalogs
        cat_locs = glob(f'{self.source_catalog_dir}/ref_*.yaml')
        cats = [intake.open_catalog(cat_loc) for cat_loc in cat_locs]

        metadata = {'source_catalog_dir': self.source_catalog_dir}

        self.source_cat = make_catalog(cats, self.source_catalog_name,
                                       cat_source_description, metadata,
                                       intake.catalog.local.YAMLFileCatalog)


    def update_model_catalogs(self, model_names=None):
        """Update model catalogs in 'source_catalogs'.

        This will update certain metadata as well as the forecast times available.

        Parameters
        ----------
        model_names: list of strings, optional
            Model names to use if user does not want to include all available model catalogs.
            Name should be like "CBOFS".

        Returns
        -------
        Nothing, but resaves either all model source catalogs or just model_names source
        catalogs into self.updated_catalog_dir with updated metadata.
        """

        if model_names is not None:
            models = model_names
        else:
            models = list(self.source_cat)

        for model in models:

            timings = list(self.source_cat[model])  # next level of submodels available in reference catalog for source_id0

            timing = timings[0]  # 'forecast' WHAT ABOUT WHEN THERE IS MORE THAN ONE FORECAST? SEARCH KEY NAMES?
                # for source_id1 in source_ids1:

            # read in model output
            ds = self.source_cat[model, timing].to_dask()

            # find metadata
            # select lon/lat for use. There may be more than one and we also want the name.
            if 'alpha_shape' in self.source_cat[model].metadata:
                # import pdb; pdb.set_trace()
                dd, alpha = self.source_cat[model].metadata['alpha_shape']
            else:
                dd, alpha = None, None
            # print(dd, alpha)
            lonkey, latkey, bbox, wkt_low, wkt_high = find_bbox(ds, dd=dd, alpha=alpha)

            # there may be more than one variable identified as time
            ds = ds.cf.guess_coord_axis()  # add metadata if guessable
            try:
                tkey = ds.cf['T'].name
            except:
                tkeys = list(ds.cf[['T']].dims)
                # choose the time key that is longer
                ntimes = 0
                for tkeytest in tkeys:
                    if ds[tkeytest].size > ntimes:
                        tkey = tkeytest
                        ntimes = max(ntimes, ds[tkey].size)

            # metadata for overall source_id0
            metadata0 = {
                'calculated_grid_dim': {'lon': ds[lonkey].shape,
                             'lat': ds[latkey].shape},
                'geospatial_bounds_low': wkt_low,
                'geospatial_bounds_high': wkt_high,
                'bounding_box': bbox
            }

            # metadata for source_id0, source_id1
            metadata1 = {
                'calculated_start_datetime': str(ds[tkey][0].values),
                'calculated_end_datetime': str(ds[tkey][-1].values),
                'calculated_dt (s)': float(ds[tkey].diff(dim=tkey).dt.seconds[0]),
            }

            # add Dataset metadata to specific source metadata
            # change metadata attributes to strings so catalog doesn't barf on them
            for attr in ds.attrs:
                self.source_cat[model, timing].metadata[attr] = str(ds.attrs[attr])

            # add 0th level metadata to 0th level model entry
            self.source_cat[model].metadata.update(metadata0)

            # add next level metadata to next level model entry
            self.source_cat[model, timing].metadata.update(metadata1)

            cats = [self.source_cat[model][timing] for timing in timings]
            make_catalog(cats,
                         model,
                         self.source_cat[model].description,
                         self.source_cat[model].metadata,
                         'opendap',
                         f'{self.updated_catalog_dir}/ref_{model.lower()}.yaml'
                        )


    def setup_updated_catalog(self, model_names=None):
        """Setup updated catalog for models.

        Parameters
        ----------
        model_names: list of strings, optional
            Model names to use if user does not want to include all available model catalogs.
            Name should be like "CBOFS".

        Returns
        -------
        Nothing, but makes the updated catalog file.
        """

        self.update_model_catalogs(model_names=model_names)

        cat_description = 'Updated catalog for models.'

        # open catalogs
        if model_names is None:
            cat_locs = glob(f'{self.updated_catalog_dir}/ref_*.yaml')
        else:
            cat_locs = [f'{self.updated_catalog_dir}/ref_{model_name.lower()}.yaml' for model_name in model_names]
        cats = [intake.open_catalog(cat_loc) for cat_loc in cat_locs]

        metadata = {'source_catalog_dir': self.source_catalog_dir,
                    'source_catalog_name': self.source_catalog_name,
                    'updated_catalog_dir': self.updated_catalog_dir}

        return make_catalog(cats, self.updated_catalog_name,
                            cat_description, metadata,
                            intake.catalog.local.YAMLFileCatalog)


    def run_updated_cat(self, updated_catalog_name='updated_catalog.yaml',
                        make_updated_catalog=True, model_names=None):
        """Find and return updated catalog.

        This runs even if `self.updated_cat` has previously been found, and overwrites it.

        Parameters
        ----------
        updated_catalog_name: str, optional
            user can input catalog name to use in place of "updated_catalog.yaml".
        make_updated_catalog: str, optional
            Default of True assumes that user does want to updated the forecast catalog
            metadata.
        model_names: list, optional
            User can specify model names to use in making `updated_cat`. If no names are
            input, the available source_catalog file names will be used.

        Returns
        -------
        Nothing, but sets up `self._updated_cat`.
        """

        # version control these catalogs by datetime
        self.updated_catalog_dir = f'{self.cat_updated_base}/{self.time_ref.isoformat()}'
        os.makedirs(self.updated_catalog_dir, exist_ok=True)
        self.updated_catalog_name = f'{self.updated_catalog_dir}/{updated_catalog_name}'

        # Create or update catalog
        if os.path.exists(self.updated_catalog_name) and not make_updated_catalog:
            updated_cat = intake.open_catalog(self.updated_catalog_name)

        else:  # otherwise, make it
            updated_cat = self.setup_updated_catalog(model_names=model_names)

        self._updated_cat = updated_cat

        return self._updated_cat


    @property
    def updated_cat(self):
        """Run run_updated_cat with default options.

        If you want to choose input options, use `run_updated_cat()` directly.

        Returns
        -------
        self.updated_cat
        """

        if not hasattr(self, "_updated_cat"):
            self._updated_cat = self.run_updated_cat()
        return self._updated_cat


    def _make_user_catalog(self, model, timing, start_date=None, end_date=None,
                           filetype='fields', treat_last_day_as_forecast=False):
        """Make a user catalog entry.

        Parameters
        ----------
        model: str
            Name of model, e.g., CBOFS
        timing: str
            Which timing to use. Normally "forecast", "nowcast", or "hindcast", if
            available for model, but could have different names and/or options.
            Find model options available with `list(self.source_cat[model])` or
            `list(self.updated_cat[model])`.
        start_date, end_date: datetime-interpretable str or pd.Timestamp, optional
            If model has an aggregated link for timing, start_date and end_date
            are not used. Otherwise they should be input. Only year-month-day
            will be used in date. end_date is inclusive.
        filetype: str, optional
            Which filetype to use. Every NOAA OFS model has "fields" available, but some have "regulargrid"
            or "2ds" also. This availability information is in the source catalog for the model under
            `filetypes` metadata. Default is "fields".
        treat_last_day_as_forecast: bool, optional
            If True, then date is the last day of the time period being sought and the forecast files
            should be brought in along with the nowcast files, to get the model output the length of the
            forecast out in time. The forecast files brought in will have the latest timing cycle of the
            day that is available. If False, all nowcast files (for all timing cycles) are brought in.

        Returns
        -------
        Source associated with the catalog entry.
        """

        # use updated_cat unless hasn't been run in which case use source_cat
        if hasattr(self, "_updated_cat"):
            ref_cat = self.updated_cat
        else:
            ref_cat = self.source_cat

        # urlpath is None or a list of filler files if the filepaths need to be determined
        if (ref_cat[model, timing].urlpath is None or isinstance(ref_cat[model, timing].urlpath, list)) \
            and 'catloc' in ref_cat[model, timing].metadata:
            if 'pattern' in ref_cat[model, timing].metadata:
                pattern = ref_cat[model, timing].metadata['pattern']
            else:
                pattern = None

            # make sure necessary variables are present
            assertion = f'You need to provide a `start_date` and `end_date` for finding the relevant model output locations.\nFor {model} and {timing}, the `overall_start_date` is: {ref_cat[model, timing].metadata["overall_start_datetime"]} `overall_end_date` is: {ref_cat[model, timing].metadata["overall_end_datetime"]}.'  # noqa
            assert start_date is not None and end_date is not None, assertion

            if "filetype" in ref_cat[model, timing].metadata:
                model_filetypes = ref_cat[model, timing].metadata["filetype"]
                assertion = f'Filetype for {model} and {timing} must be one of: {model_filetypes}.'  # noqa
                assert filetype in model_filetypes, assertion
            assertion = f'If timing is "hindcast", `treat_last_day_as_forecast` must be False because the forecast files are not available. `timing`=={timing}.'  # noqa
            if timing == 'hindcast':
                assert not treat_last_day_as_forecast, assertion

            catloc = ref_cat[model, timing].metadata['catloc']

            # loop over dates
            filelocs = []
            for date in pd.date_range(start=start_date, end=end_date, freq='1D'):
                if treat_last_day_as_forecast and (date == end_date):
                    treat = True
                else:
                    treat = False
                filelocs.extend(agg_for_date(date, catloc, filetype, treat_last_day_as_forecast=treat, pattern=pattern))

            source = ref_cat[model, timing](urlpath=filelocs)#[:2])

        # urlpath is already available if the link is consistent in time
        else:
            source = ref_cat[model, timing]

        # update source's info with model name since user would probably prefer this over timing?
        # also other metadata to bring into user catalog
        source.name = f'{model}-{timing}'
        source.description = f'{model}-{timing}'
        if "filetype" in ref_cat[model, timing].metadata:
            source.name += f'-{filetype}'
            source.description += f'-{filetype}'
        if treat_last_day_as_forecast:
            source.name += '-with_forecast'
            source.description += '-with_forecast'

        metadata = {'model': model, 'timing': timing, 'start_date': start_date,
                    'end_date': end_date, 'filetype': filetype,
                    'treat_last_day_as_forecast': treat_last_day_as_forecast}
        source.metadata.update(metadata)

        return source


    def setup_user_cat(self, option_dicts):
        """Setup user catalog.

        Parameters
        ----------
        option_dicts: list of dicts
            Which options to use for each user catalog source. Must include: model, timing.
            Can include: start_date, end_date, filetype, treat_last_day_as_forecast.

        Returns
        -------
        Nothing, but sets up `self.user_cat`.
        """

        self.user_catalog_dir = self.cat_user_base
        self.user_catalog_name = f'{self.user_catalog_dir}/{self.time_ref.isoformat()}.yaml'

        sources = []
        for option_dict in option_dicts:
            source = self._make_user_catalog(**option_dict)
            sources.append(source)

        metadata = {'source_catalog_dir': self.source_catalog_dir,
                    'source_catalog_name': self.source_catalog_name}

        if hasattr(self, "_updated_cat"):
            metadata['updated_catalog_dir'] = self.updated_catalog_dir

        # make combined user_cat
        self.user_cat = make_catalog(sources, 'User catalog', 'User-made catalog.', metadata,
                                     'opendap', cat_path=self.user_catalog_name)
