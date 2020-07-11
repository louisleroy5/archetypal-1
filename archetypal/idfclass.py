################################################################################
# Module: idfclass.py
# Description: Various functions for processing of EnergyPlus models and
#              retrieving results in different forms
# License: MIT, see full license in LICENSE.txt
# Web: https://github.com/samuelduchesne/archetypal
################################################################################
import copy
import datetime
import hashlib
import inspect
import itertools
import logging as lg
import os
import platform
import re
import subprocess
import sys
import time
import warnings
from collections import defaultdict, OrderedDict
from io import StringIO
from itertools import compress, chain
from math import isclose
from sqlite3 import OperationalError
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import Any

import eppy
import eppy.modeleditor
import geomeppy
import pandas as pd
from deprecation import deprecated
from eppy.EPlusInterfaceFunctions import parse_idd
from eppy.EPlusInterfaceFunctions.eplusdata import Eplusdata, Idd, removecomment
from eppy.bunch_subclass import BadEPFieldError
from eppy.easyopen import getiddfile
from eppy.idfreader import iddversiontuple
from eppy.modeleditor import IDDNotSetError, namebunch, newrawobject
from geomeppy.patches import EpBunch, idfreader1, obj2bunch
from pandas.errors import ParserError
from path import Path, TempDir
from tqdm import tqdm

import archetypal
import archetypal.settings
from archetypal import (
    log,
    settings,
    EnergyPlusProcessError,
    ReportData,
    EnergySeries,
    close_logger,
    EnergyPlusVersionError,
    get_eplus_dirs,
    EnergyPlusWeatherError,
)
from archetypal.utils import _unpack_tuple, parse, extend_class, get_eplus_basedirs


class IDF(geomeppy.IDF):
    """Class for loading and parsing idf models and running simulations and
    retrieving results.

    Wrapper over the geomeppy.IDF class and subsequently the
    eppy.modeleditor.IDF class
    """

    # dependencies: dict of <dependant value: independant value>
    _dependencies = {
        "iddname": ["idfname", "as_version"],
        "file_version": ["idfname"],
        "idd_info": ["iddname", "idfname"],
        "idd_index": ["iddname", "idfname"],
        "idd_version": ["iddname", "idfname"],
        "idfobjects": ["iddname", "idfname"],
        "block": ["iddname", "idfname"],
        "model": ["iddname", "idfname"],
        "sql": ["as_version", "annual", "design_day", "epw", "idfname", "iddname"],
        "htm": ["as_version", "annual", "design_day", "epw", "idfname", "iddname"],
        "schedules_dict": ["idfobjects"],
    }
    _independant_vars = set(chain(*list(_dependencies.values())))
    _dependant_vars = set(_dependencies.keys())

    _initial_postition = itertools.count(start=1)

    def __new__(cls, idfname=None, **kwargs):
        existing = None
        if existing:
            return existing
        idf = super(IDF, cls).__new__(cls)
        return idf

    def _reset_dependant_vars(self, name):
        _reverse_dependencies = {}
        for k, v in self._dependencies.items():
            for x in v:
                _reverse_dependencies.setdefault(x, []).append(k)
        for var in _reverse_dependencies[name]:
            super().__setattr__(f"_{var}", None)

    def __setattr__(self, key, value):
        propobj = getattr(self.__class__, key, None)
        if isinstance(propobj, property):
            if propobj.fset is None:
                raise AttributeError("Cannot set attribute")
                # self.__set_on_dependencies(key.strip("_"), value)
            else:
                propobj.fset(self, value)
                self.__set_on_dependencies(key, value)
        else:
            self.__set_on_dependencies(key, value)

    def __set_on_dependencies(self, key, value):
        if key in self._dependant_vars:
            raise AttributeError("Cannot set this value.")
        if key in self._independant_vars:
            self._reset_dependant_vars(key)
            key = f"_{key}"
        super(IDF, self).__setattr__(key, value)

    def __init__(
        self,
        idfname=None,
        epw=None,
        as_version=None,
        annual=False,
        design_day=False,
        expandobjects=True,
        verbose="v",
        readvars=True,
        prep_outputs=True,
        include=None,
        custom_processes=None,
        simulname=None,
        output_suffix="L",
        epmacro=True,
        keep_data=True,
        keep_data_err=False,
        position=0,
        **kwargs,
    ):
        """
        Args:
            idfname (str _TemporaryFileWrapper): The idf model filename.
            epw (str or Path): The weather-file

        EnergyPlus args:
            output_directory=None,
            as_version=None,
            prep_outputs=True,
            include=None,
            keep_original=True,
        """
        # Set independents to there original values
        self._idfname = idfname
        self._epw = epw
        self._custom_processes = custom_processes
        self._include = include
        self._keep_data_err = keep_data_err
        self._keep_data = keep_data
        self._simulname = simulname
        self._output_suffix = output_suffix
        self._verbose = verbose
        self._readvars = readvars
        self._expandobjects = expandobjects
        self._epmacro = epmacro
        self._design_day = design_day
        self._annual = annual
        self._prep_outputs = prep_outputs
        self._as_version = parse(as_version)
        self._position = position or next(IDF._initial_postition)

        # Set dependants to None
        self._file_version = None
        self._iddname = None
        self._idd_info = None
        self._idd_index = None
        self._idd_version = None
        self._idfobjects = None
        self._block = None
        self._model = None
        self._sql = None
        self._htm = None
        self._original_ep_version = None
        self._schedules_dict = None
        self._outputs = None

        self.load_kwargs = dict(epw=epw, **kwargs)

        self._original_idfname = (
            idfname if idfname else StringIO(f"VERSION, {latest_energyplus_version()};")
        )

        # Move to output_directory, if we want to keep the original file intact.
        if settings.use_cache:
            previous_file = self.output_directory / self.name
            if previous_file.exists():
                # We have a transitioned or cached file here; Load this one.
                self.idfname = previous_file
            else:
                if not isinstance(self.idfname, StringIO):
                    self.idfname = self.idfname.copy(self.output_directory)

        try:
            # load the idf object by asserting self.idd_info
            assert self.idd_info
            self.outputtype = "standard"
        except Exception as e:
            raise e
        finally:
            # Set model outputs
            self._outputs = Outputs(idf=self)
            if self.prep_outputs:
                (
                    self._outputs.add_basics()
                    .add_umi_template_outputs()
                    .add_custom(outputs=self.prep_outputs)
                    .add_profile_gas_elect_ouputs()
                    .apply()
                )

    def __str__(self):
        return self.name

    def setiddname(self, iddname, testing=False):
        """Set the path to the EnergyPlus IDD for the version of EnergyPlus
        which is to be used by eppy.

        Args:
            iddname (str): Path to the IDD file.
            testing:
        """
        self.iddname = iddname
        self.idd_info = None
        self.block = None

    def valid_idds(self):
        """Returns all valid idd version numbers found in IDFVersionUpdater folder"""
        iddnames = self.idfversionupdater_dir.files("*.idd")
        return [
            parse((re.match("V(.*)-Energy\+", idd.stem).groups()[0]))
            for idd in iddnames
        ]

    def read(self):
        """
        Read the IDF file and the IDD file. If the IDD file had already been
        read, it will not be read again.

        Read populates the following data structures:

        - idfobjects : list
        - model : list
        - idd_info : list
        - idd_index : dict

        """
        if self.getiddname() is None:
            errortxt = (
                "IDD file needed to read the idf file. "
                "Set it using IDF.setiddname(iddfile)"
            )
            raise IDDNotSetError(errortxt)
        readout = idfreader1(
            self.idfname, self.iddname, self, commdct=self.idd_info, block=self.block
        )
        (self.idfobjects, block, self.model, idd_info, idd_index, idd_version) = readout
        self.setidd(idd_info, idd_index, block, idd_version)

    def getiddname(self):
        """Get the name of the current IDD used by eppy.

        Returns
        -------
        str

        """
        return self.iddname

    def setidd(self, iddinfo, iddindex, block, idd_version):
        """Set the IDD to be used by eppy.

        Parameters
        ----------
        iddinfo : list
            Comments and metadata about fields in the IDD.
        block : list
            Field names in the IDD.

        """
        self.idd_info = iddinfo
        self.block = block
        self.idd_index = iddindex
        self.idd_version = idd_version

    @classmethod
    def cached_file(cls, idfname, **kwargs):
        cache_filename = hash_model(idfname, **kwargs)
        output_directory = settings.cache_folder / cache_filename
        return output_directory / os.extsep.join([cache_filename + "idfs", "dat"])

    @property
    def original_idfname(self):
        return self._original_idfname

    @property
    def idfname(self):
        """The path of the active (parsed) idf model. If `settings.use_cache ==
        True`, then this path will point to `settings.cache_folder`. See
        :meth:`~archetypal.utils.config`"""
        if self._idfname is None:
            idfname = StringIO(f"VERSION, {latest_energyplus_version()};")
            idfname.seek(0)
            self._idfname = idfname
        else:
            if isinstance(self._idfname, StringIO):
                self._idfname.seek(0)
            else:
                self._idfname = Path(self._idfname).expand()
        return self._idfname

    @idfname.setter
    def idfname(self, value):
        self._idfname = Path(value).expand()

    @property
    def block(self):
        if self._block is None:
            _, block, _, _, _, _ = idfreader1(
                self.idfname, self.iddname, self, commdct=None, block=None
            )
            self._block = block
        return self._block

    @property
    def idd_info(self):
        if self._idd_info is None:
            _, _, _, idd_info, _, _ = idfreader1(
                self.idfname, self.iddname, self, commdct=None, block=None
            )
            self._idd_info = idd_info
        return self._idd_info

    @property
    def idd_index(self):
        if self._idd_index is None:
            _, _, _, _, idd_index, _ = idfreader1(
                self.idfname, self.iddname, self, commdct=None, block=None
            )
            self._idd_index = idd_index
        return self._idd_index

    @property
    def idd_version(self):
        if self._idd_version is None:
            versiontuple = iddversiontuple(self.iddname)
            self._idd_version = versiontuple
        return self._idd_version

    @property
    def idfobjects(self):
        if self._idfobjects is None:
            idfobjects, _, _, _, _, idd_version = idfreader1(
                self.idfname, self.iddname, self, commdct=None, block=None
            )
            self._idfobjects = idfobjects
        return self._idfobjects

    @property
    def model(self):
        if self._model is None:
            _, _, model, _, _, idd_version = idfreader1(
                self.idfname, self.iddname, self, commdct=None, block=None
            )
            self._model = model
        return self._model

    @property
    def epw(self):
        return self._epw

    @epw.setter
    def epw(self, value):
        self._epw = Path(value).expand()

    @property
    def iddname(self):
        if self._iddname is None:
            if self.file_version > self.as_version:
                raise EnergyPlusVersionError(
                    f"{self.as_version} cannot be lower then "
                    f"the version number set in the model"
                )
            elif self.file_version < self.as_version:
                self.upgrade(to_version=self.as_version)
            idd_filename = Path(getiddfile(str(self.file_version))).expand()
            if not idd_filename.exists():
                # Try finding the one in IDFVersionsUpdater
                idd_filename = (
                    self.idfversionupdater_dir / f"V"
                    f"{self.file_version.dash}-Energy+.idd"
                ).expand()
            self._iddname = idd_filename
        return self._iddname

    @property
    def file_version(self):
        if self._file_version is None:
            return parse(get_idf_version(self.idfname))

    @property
    def custom_processes(self):
        return self._custom_processes

    @property
    def include(self):
        return self._include

    @property
    def keep_data_err(self):
        return self._keep_data_err

    @property
    def keep_data(self):
        return self._keep_data

    @property
    def simulname(self):
        return self._simulname

    @property
    def output_suffix(self):
        return self._output_suffix

    @property
    def verbose(self):
        return self._verbose

    @property
    def expandobjects(self):
        return self._expandobjects

    @property
    def readvars(self):
        return self._readvars

    @property
    def epmacro(self):
        return self._epmacro

    @property
    def design_day(self):
        return self._design_day

    @property
    def annual(self):
        return self._annual

    @property
    def prep_outputs(self):
        """Bool or set list of custom outputs"""
        return self._prep_outputs

    # @property
    # def original_ep_version(self):
    #     if self._original_ep_version is None:
    #         ep_version = parse(get_idf_version(self.original_idfname))
    #         self._original_ep_version = ep_version
    #     return self._original_ep_version

    @property
    def as_version(self):
        if self._as_version is None:
            self._as_version = latest_energyplus_version()
        return self._as_version

    @as_version.setter
    def as_version(self, value):
        # Parse value and check if above or bellow
        parsed_version = parse(value)
        if parsed_version not in self.valid_idds():
            raise EnergyPlusVersionError(
                f"{parsed_version} is not valid. Choices are {self.valid_idds()}"
            )
        self._as_version = parsed_version

    @property
    def position(self):
        return self._position

    @property
    def idfversionupdater_dir(self):
        return (
            get_eplus_dirs(settings.ep_version) / "PreProcess" / "IDFVersionUpdater"
        ).expand()

    @property
    def output_directory(self):
        """Returns the output directory based on the hashing of the original file (
        before transitions or modifications)."""
        return self.get_output_directory(self.original_idfname).expand()

    @property
    def output_prefix(self):
        return hash_model(
            self,
            epw=self.epw,
            annual=self.annual,
            design_day=self.design_day,
            readvars=self.readvars,
            ep_version=self.as_version,
        )

    @property
    def idf_version(self):
        return self.file_version

    @property
    def name(self):
        if isinstance(self.idfname, StringIO):
            return str(self.idfname)
        return self.idfname.basename()

    @property
    def sql(self):
        """Get the sql table report"""
        if self._sql is None:
            try:
                sql_dict = get_report(
                    self.idfname,
                    self.simulation_dir,
                    output_report="sql",
                    output_prefix=self.output_prefix,
                )
            except FileNotFoundError:
                # check if htm output is in file
                sql_object = self.anidfobject(
                    key="Output:SQLite".upper(), Option_Type="SimpleAndTabular"
                )
                if sql_object not in self.idfobjects["Output:SQLite".upper()]:
                    self.addidfobject(sql_object)
                return self.simulate().sql
            except Exception as e:
                raise e
            else:
                self._sql = sql_dict
        return self._sql

    @property
    def htm(self):
        """Get the htm table report"""
        if self._htm is None:
            try:
                htm_dict = get_report(
                    self.idfname,
                    self.simulation_dir,
                    output_report="htm",
                    output_prefix=self.output_prefix,
                )
            except FileNotFoundError:
                return self.simulate().htm
            else:
                self._htm = htm_dict
        return self._htm

    @property
    def sql_file(self):
        """Get the sql file path"""
        try:
            files = self.simulation_dir.files("*out.sql")
        except FileNotFoundError:
            return self.simulate().sql_file
        else:
            return files[0].expand()

    @property
    def area_conditioned(self):
        """Returns the total conditioned area of a building (taking into account
        zone multipliers
        """
        area = 0
        zones = self.idfobjects["ZONE"]
        zone: EpBunch
        for zone in zones:
            for surface in zone.zonesurfaces:
                if hasattr(surface, "tilt"):
                    if surface.tilt == 180.0:
                        part_of = int(zone.Part_of_Total_Floor_Area.upper() != "NO")
                        multiplier = float(
                            zone.Multiplier if zone.Multiplier != "" else 1
                        )

                        area += surface.area * multiplier * part_of
        return area

    @property
    def partition_ratio(self):
        """The number of lineal meters of partitions (Floor to ceiling) present
        in average in the building floor plan by m2.
        """
        partition_lineal = 0
        zones = self.idfobjects["ZONE"]
        zone: EpBunch
        for zone in zones:
            for surface in [
                surf
                for surf in zone.zonesurfaces
                if surf.key.upper() not in ["INTERNALMASS", "WINDOWSHADINGCONTROL"]
            ]:
                if hasattr(surface, "tilt"):
                    if (
                        surface.tilt == 90.0
                        and surface.Outside_Boundary_Condition != "Outdoors"
                    ):
                        multiplier = float(
                            zone.Multiplier if zone.Multiplier != "" else 1
                        )
                        partition_lineal += surface.width * multiplier

        return partition_lineal / self.area_conditioned

    @property
    def simulation_files(self):
        return self.simulation_dir.files()

    @property
    def simulation_dir(self):
        """The path where simulation results are stored"""
        try:
            return (self.output_directory / self.output_prefix).expand()
        except AttributeError:
            return Path()

    @property
    def schedules_dict(self):
        if self._schedules_dict is None:
            self._schedules_dict = self.get_all_schedules()
        return self._schedules_dict

    @property
    def outputs(self):
        return self._outputs

    @property
    def day_of_week_for_start_day(self):
        """Get day of week for start day for the first found RUNPERIOD"""
        import calendar

        day = self.idfobjects["RUNPERIOD"][0]["Day_of_Week_for_Start_Day"]

        if day.lower() == "sunday":
            return calendar.SUNDAY
        elif day.lower() == "monday":
            return calendar.MONDAY
        elif day.lower() == "tuesday":
            return calendar.TUESDAY
        elif day.lower() == "wednesday":
            return calendar.WEDNESDAY
        elif day.lower() == "thursday":
            return calendar.THURSDAY
        elif day.lower() == "friday":
            return calendar.FRIDAY
        elif day.lower() == "saturday":
            return calendar.SATURDAY
        else:
            return 0

    def simulate(self, **kwargs):
        """Execute EnergyPlus. Does not return anything. See
        :meth:`simulation_files`, :meth:`processed_results` for simulation outputs.

        Keyword Args:
            eplus_file (str): path to the idf file.
            weather_file (str): path to the EPW weather file.
            output_directory (str, optional): path to the output folder.
            ep_version (str, optional): EnergyPlus executable version to use, eg: 9-2-0
            output_report: 'sql' or 'htm'
            prep_outputs (bool or list, optional): if True, meters and variable
                outputs will be appended to the idf files. Can also specify custom
                outputs as list of ep-object outputs.
            simulname (str): The name of the simulation. (Todo: Currently not
            implemented).
            keep_data (bool): If True, files created by EnergyPlus are saved to the
                output_directory.
            annual (bool): If True then force annual simulation (default: False)
            design_day (bool): Force design-day-only simulation (default: False)
            epmacro (bool): Run EPMacro prior to simulation (default: False)
            expandobjects (bool): Run ExpandObjects prior to simulation (default:
                True)
            readvars (bool): Run ReadVarsESO after simulation (default: False)
            output_prefix (str, optional): Prefix for output file names.
            output_suffix (str, optional): Suffix style for output file names
                (default: L) Choices are:
                    - L: Legacy (e.g., eplustbl.csv)
                    - C: Capital (e.g., eplusTable.csv)
                    - D: Dash (e.g., eplus-table.csv)
            version (bool, optional): Display version information (default: False)
            verbose (str): Set verbosity of runtime messages (default: v) v: verbose
                q: quiet
            keep_data_err (bool): If True, errored directory where simulation
            occurred is
                kept.
            include (str, optional): List input files that need to be copied to the
                simulation directory. If a string is provided, it should be in a glob
                form (see :meth:`pathlib.Path.glob`).
            process_files (bool): If True, process the output files and load to a
                :class:`~pandas.DataFrame`. Custom processes can be passed using the
                :attr:`custom_processes` attribute.
            custom_processes (dict(Callback)): if provided, it has to be a
                dictionary with the keys being a glob (see
                :meth:`pathlib.Path.glob`), and
                the value a Callback taking as signature `callback(file: str,
                working_dir, simulname) -> Any` All the file matching this glob will
                be processed by this callback. Note: they will still be processed by
                pandas.read_csv (if they are csv files), resulting in duplicate. The
                only way to bypass this behavior is to add the key "*.csv" to that
                dictionary.
            return_idf (bool): If True, returns the :class:`IDF` object part of the
                return tuple.
            return_files (bool): It True, all files paths created by the EnergyPlus
                run are returned.

        Raises:
            EnergyPlusProcessError: If an issue occurs with the execution of the
                energyplus command.

        """
        # First, update keys with new values
        for key, value in kwargs.items():
            if f"_{key}" in self.__dict__.keys():
                setattr(self, key, value)

        if self.as_version != parse(self.idd_version):
            raise EnergyPlusVersionError(
                self.idfname, parse(self.idd_version), self.as_version
            )

        start_time = time.time()
        include = self.include
        if isinstance(include, str):
            include = Path().abspath().glob(include)
        elif include is not None:
            include = [Path(file) for file in include]

        # check if a weather file is defined
        if not getattr(self, "epw", None):
            raise EnergyPlusWeatherError(
                f"No weather file specified with {self}. Set 'epw' in IDF("
                f"filename, epw='weather.epw').simulate() or in IDF.simulate("
                f"epw='weather.epw')"
            )

        # run the EnergyPlus Simulation
        with TempDir(
            prefix="eplus_run_", suffix=self.output_prefix, dir=self.output_directory,
        ) as tmp:
            log(f"temporary dir ({Path(tmp).expand()}) created")
            if include:
                [file.copy(tmp) for file in include]
            try:
                tmp_file = Path(self.savecopy(tmp / self.name))
            except FileNotFoundError as e:
                log(
                    f"Something wrong with the file name. Replaced '"
                    f"{self.idfname}'with 'idf_model.idf'"
                )
                tmp_file = Path(self.savecopy(tmp / "idf_model.idf"))

            runargs = {
                "tmp": tmp,
                "eplus_file": tmp_file,
                "weather": Path(self.epw).copy(tmp),
                "output_directory": self.output_directory,
                "annual": self.annual,
                "design_day": self.design_day,
                "idd": Path(self.iddname).copy(tmp),
                "epmacro": self.epmacro,
                "expandobjects": self.expandobjects,
                "readvars": self.readvars,
                "output_prefix": self.output_prefix,
                "output_suffix": self.output_suffix,
                "version": False,
                "verbose": self.verbose,
                "ep_version": self.as_version.dash,  # must be "ep_version"
                "keep_data_err": self.keep_data_err,
                "include": self.include,
                "custom_processes": self.custom_processes,
                "position": self.position,
            }

            try:
                _run_exec(**runargs)
            except (EnergyPlusProcessError, CalledProcessError) as e:
                log(str(e), lg.ERROR)
                raise e
            except Exception as e:
                log(str(e), lg.ERROR)
                raise e
            else:
                log(
                    "EnergyPlus Completed in {:,.2f} seconds".format(
                        time.time() - start_time
                    ),
                    name=self.name,
                )

                self._set_cached_results(runargs, tmp, tmp_file)
        return self

    def savecopy(self, filename, lineendings="default", encoding="latin-1"):
        """Save a copy of the file with the filename passed.

        Args:
            filename (str): Filepath to save the file.
            lineendings (str): Line endings to use in the saved file. Options are
            'default',
                'windows' and 'unix' the default is 'default' which uses the line
                endings for the current system.
            encoding (str): Encoding to use for the saved file. The default is
            'latin-1' which
                is compatible with the EnergyPlus IDFEditor.

        Returns:
            Path: The new file path.
        """
        super(IDF, self).save(filename, lineendings, encoding)
        return Path(filename)

    def save(self, filename=None, lineendings="default", encoding="latin-1"):
        """Save the IDF as a text file with the optional filename passed. If None,
        the original_idfname of the IDF is used. Uses
        :meth:`~eppy.modeleditor.IDF.saveas`

        Args:
            filename (str): Filepath to save the file. If None then use the IDF.idfname
                parameter. Also accepts a file handle.
            lineendings (str) : Line endings to use in the saved file. Options are
            'default',
                'windows' and 'unix' the default is 'default' which uses the line
                endings for the current system.
            encoding (str): Encoding to use for the saved file. The default is
                'latin-1' which is compatible with the EnergyPlus IDFEditor.
            """
        if not settings.use_cache and filename is None:
            self.idfname = self.original_idfname
        save_path = super(IDF, self).save(
            filename=filename, lineendings=lineendings, encoding=encoding
        )
        log(f"saved '{self.name}' at '{filename if filename else self.idfname}'")
        return save_path

    def process_results(self):
        """Returns the list of processed results as defined by self.custom_processes
        as a list of tuple(file, result). A default process looks for csv files
        and tries to parse them into :class:`~pandas.DataFrame` objects.

        Returns:
            list: List of two-tuples.

        Info:
            For processed_results to work more consistently, it may be necessary to
            add the "readvars=True" parameter to :func:`IDF.simulate` as this one is
            set to false by default.

        """
        processes = {"*.csv": _process_csv}
        custom_processes = self.custom_processes
        if custom_processes:
            processes.update(custom_processes)

        try:
            results = []
            for glob, process in processes.items():
                results.extend(
                    [
                        (
                            file.basename(),
                            process(
                                file,
                                working_dir=os.getcwd(),
                                simulname=self.output_prefix,
                            ),
                        )
                        for file in self.simulation_dir.files(glob)
                    ]
                )
        except FileNotFoundError:
            self.simulate()
            return self.process_results()
        else:
            return results

    @staticmethod
    def get_output_directory(idfname, **kwargs):
        """

        Args:
            idfname (Path-like):

        Returns:

        """
        cache_filename = hash_model(idfname, **kwargs)
        output_directory = settings.cache_folder / cache_filename
        output_directory.makedirs_p()
        return output_directory

    def upgrade(self, to_version, overwrite=True, **kwargs):
        """EnergyPlus idf version updater using local transition program.

        Update the EnergyPlus simulation file (.idf) to the latest available
        EnergyPlus version installed on this machine. Optionally specify a version
        (eg.: "9-2-0") to aim for a specific version. The output will be the path of
        the updated file. The run is multiprocessing_safe.

        Hint:
            If attempting to upgrade an earlier version of EnergyPlus ( pre-v7.2.0),
            specific binaries need to be downloaded and copied to the
            EnergyPlus*/PreProcess/IDFVersionUpdater folder. More info at
            `Converting older version files
            <http://energyplus.helpserve.com/Knowledgebase/List/Index/46
            /converting-older-version-files>`_ .

        Args:
            to_version (str, optional): EnergyPlus version in the form "X-X-X".
            overwrite (bool): If True, original idf file is overwritten with new
                transitioned file.

        Keyword Args:
            Same as :class:`IDF`

        Raises:
            EnergyPlusProcessError: If version updater fails.
            EnergyPlusVersionError:
            CalledProcessError:
        """
        if self.file_version == to_version:
            return
        if self.file_version > to_version:
            raise EnergyPlusVersionError(self.name, self.idf_version, to_version)
        else:
            # execute transitions
            with TemporaryDirectory(
                prefix="transition_run_", suffix=None, dir=self.output_directory
            ) as tmp:
                # Move to temporary transition_run folder
                log(f"temporary dir ({Path(tmp).expand()}) created", lg.DEBUG)
                idf_file = Path(
                    self.idfname.copy(tmp)
                ).abspath()  # copy and return abspath
                try:
                    self._execute_transitions(idf_file, to_version, **kwargs)
                except (CalledProcessError, EnergyPlusProcessError) as e:
                    raise e

                # retrieve transitioned file
                for f in Path(tmp).files("*.idfnew"):
                    if overwrite:
                        file = f.copy(self.output_directory / idf_file.basename())
                    else:
                        file = f.copy(self.output_directory)

            try:
                self.idfname = file
            except (NameError, UnboundLocalError):
                raise EnergyPlusProcessError(
                    cmd="IDF.upgrade",
                    stderr=f"An error occurred during transitioning",
                    idf=self.name,
                )

    @deprecated(
        deprecated_in="1.4",
        removed_in="1.5",
        current_version=archetypal.__version__,
        details="Use IDF.simulate() method instead",
    )
    def run_eplus(self, **kwargs):
        """wrapper around the :meth:`archetypal.idfclass.run_eplus` method.

        If weather file is defined in the IDF object, then this field is
        optional. By default, will load the sql in self.sql.

        Args:
            kwargs:

        Returns:
            The output report or the sql file loaded as a dict of DataFrames.
        """
        self.__dict__.update(kwargs)
        results = run_eplus(**self.__dict__)
        if kwargs.get("output_report", "sql") == "sql":
            # user simply wants the sql
            self._sql = results
            return self._sql
        elif kwargs.get("output_report") == "sql_file":
            self._sql_file = results
            return results

    def wwr(self, azimuth_threshold=10, round_to=None):
        """Returns the Window-to-Wall Ratio by major orientation for the IDF
        model. Optionally round up the WWR value to nearest value (eg.: nearest
        10).

        Args:
            azimuth_threshold (int): Defines the incremental major orientation
                azimuth angle. Due to possible rounding errors, some surface
                azimuth can be rounded to values different than the main
                directions (eg.: 89 degrees instead of 90 degrees). Defaults to
                increments of 10 degrees.
            round_to (float): Optionally round the WWR value to nearest value
                (eg.: nearest 10). If None, this is ignored and the float is
                returned.

        Returns:
            (pd.DataFrame): A DataFrame with the total wall area, total window
            area and WWR for each main orientation of the building.
        """
        import math

        def roundto(x, to=10.0):
            """Rounds up to closest `to` number"""
            if to and not math.isnan(x):
                return int(round(x / to)) * to
            else:
                return x

        total_wall_area = defaultdict(int)
        total_window_area = defaultdict(int)

        zones = self.idfobjects["ZONE"]
        zone: EpBunch
        for zone in zones:
            multiplier = float(zone.Multiplier if zone.Multiplier != "" else 1)
            for surface in [
                surf
                for surf in zone.zonesurfaces
                if surf.key.upper() not in ["INTERNALMASS", "WINDOWSHADINGCONTROL"]
            ]:
                if isclose(surface.tilt, 90, abs_tol=10):
                    if surface.Outside_Boundary_Condition == "Outdoors":
                        surf_azim = roundto(surface.azimuth, to=azimuth_threshold)
                        total_wall_area[surf_azim] += surface.area * multiplier
                for subsurface in surface.subsurfaces:
                    if isclose(subsurface.tilt, 90, abs_tol=10):
                        if subsurface.Surface_Type.lower() == "window":
                            surf_azim = roundto(
                                subsurface.azimuth, to=azimuth_threshold
                            )
                            total_window_area[surf_azim] += subsurface.area * multiplier
        # Fix azimuth = 360 which is the same as azimuth 0
        total_wall_area[0] += total_wall_area.pop(360, 0)
        total_window_area[0] += total_window_area.pop(360, 0)

        # Create dataframe with wall_area, window_area and wwr as columns and azimuth
        # as indexes
        df = pd.DataFrame(
            {"wall_area": total_wall_area, "window_area": total_window_area}
        ).rename_axis("Azimuth")
        df["wwr"] = df.window_area / df.wall_area
        df["wwr_rounded_%"] = (df.window_area / df.wall_area * 100).apply(
            lambda x: roundto(x, to=round_to)
        )
        return df

    def space_heating_profile(
        self,
        units="kWh",
        energy_out_variable_name=None,
        name="Space Heating",
        EnergySeries_kwds=None,
    ):
        """
        Args:
            units (str): Units to convert the energy profile to. Will detect the
                units of the EnergyPlus results.
            energy_out_variable_name (list-like): a list of EnergyPlus Variable
                names.
            name (str): Name given to the EnergySeries.
            EnergySeries_kwds (dict, optional): keywords passed to
                :func:`EnergySeries.from_sqlite`

        Returns:
            EnergySeries
        """
        if EnergySeries_kwds is None:
            EnergySeries_kwds = {}
        start_time = time.time()
        if energy_out_variable_name is None:
            energy_out_variable_name = (
                "Air System Total Heating Energy",
                "Zone Ideal Loads Zone Total Heating Energy",
            )
        series = self._energy_series(
            energy_out_variable_name, units, name, EnergySeries_kwds=EnergySeries_kwds
        )
        log(
            "Retrieved Space Heating Profile in {:,.2f} seconds".format(
                time.time() - start_time
            )
        )
        return series

    def space_cooling_profile(
        self,
        units="kWh",
        energy_out_variable_name=None,
        name="Space Cooling",
        EnergySeries_kwds=None,
    ):
        """
        Args:
            units (str): Units to convert the energy profile to. Will detect the
                units of the EnergyPlus results.
            energy_out_variable_name (list-like): a list of EnergyPlus
            name (str): Name given to the EnergySeries.
            EnergySeries_kwds (dict, optional): keywords passed to
                :func:`EnergySeries.from_sqlite`

        Returns:
            EnergySeries
        """
        if EnergySeries_kwds is None:
            EnergySeries_kwds = {}
        start_time = time.time()
        if energy_out_variable_name is None:
            energy_out_variable_name = (
                "Air System Total Cooling Energy",
                "Zone Ideal Loads Zone Total Cooling Energy",
            )
        series = self._energy_series(
            energy_out_variable_name, units, name, EnergySeries_kwds=EnergySeries_kwds
        )
        log(
            "Retrieved Space Cooling Profile in {:,.2f} seconds".format(
                time.time() - start_time
            )
        )
        return series

    def service_water_heating_profile(
        self,
        units="kWh",
        energy_out_variable_name=None,
        name="Space Heating",
        EnergySeries_kwds=None,
    ):
        """
        Args:
            units (str): Units to convert the energy profile to. Will detect the
                units of the EnergyPlus results.
            energy_out_variable_name (list-like): a list of EnergyPlus Variable
                names.
            name (str): Name given to the EnergySeries.
            EnergySeries_kwds (dict, optional): keywords passed to
                :func:`EnergySeries.from_sqlite`

        Returns:
            EnergySeries
        """
        if EnergySeries_kwds is None:
            EnergySeries_kwds = {}
        start_time = time.time()
        if energy_out_variable_name is None:
            energy_out_variable_name = ("WaterSystems:EnergyTransfer",)
        series = self._energy_series(
            energy_out_variable_name, units, name, EnergySeries_kwds=EnergySeries_kwds
        )
        log(
            "Retrieved Service Water Heating Profile in {:,.2f} seconds".format(
                time.time() - start_time
            )
        )
        return series

    def custom_profile(
        self,
        energy_out_variable_name,
        name,
        units="kWh",
        prep_outputs=None,
        EnergySeries_kwds=None,
    ):
        """
        Args:
            energy_out_variable_name (list-like): a list of EnergyPlus
            name (str): Name given to the EnergySeries.
            units (str): Units to convert the energy profile to. Will detect the
                units of the EnergyPlus results.
            prep_outputs:
            EnergySeries_kwds (dict, optional): keywords passed to
                :func:`EnergySeries.from_sqlite`

        Returns:
            EnergySeries
        """
        if EnergySeries_kwds is None:
            EnergySeries_kwds = {}
        start_time = time.time()
        series = self._energy_series(
            energy_out_variable_name,
            units,
            name,
            prep_outputs,
            EnergySeries_kwds=EnergySeries_kwds,
        )
        log("Retrieved {} in {:,.2f} seconds".format(name, time.time() - start_time))
        return series

    def newidfobject(self, key, **kwargs):
        """Add a new object to an idf file. The function will test if the object
        exists to prevent duplicates.

        Args:
            key (str): The type of IDF object. This must be in ALL_CAPS.
            **kwargs: Keyword arguments in the format `field=value` used to set
                fields in the EnergyPlus object.

        Example:
            >>> from archetypal import IDF
            >>> IDF.newidfobject(
            >>>     key="Schedule:Constant".upper(),
            >>>     Name="AlwaysOn",
            >>>     Schedule_Type_Limits_Name="",
            >>>     Hourly_Value=1,
            >>> )

        Returns:
            EpBunch: the object
        """
        # get list of objects
        existing_objs = self.idfobjects[key]  # a list

        # create new object
        try:
            new_object = self.anidfobject(key, **kwargs)
        except BadEPFieldError as e:
            if str(e) == "unknown field Key_Name":
                # Try backwards compatibility with EnergyPlus < 9.0.0
                name = kwargs.pop("Key_Name")
                kwargs["Name"] = name
                new_object = self.anidfobject(key, **kwargs)
            else:
                log(f"Could not add object {key} because of: {e}", lg.WARNING)
        else:
            # If object is supposed to be 'unique-object', deletes all objects to be
            # sure there is only one of them when creating new object
            # (see following line)
            if "unique-object" in set().union(
                *(d.objidd[0].keys() for d in existing_objs)
            ):
                for obj in existing_objs:
                    self.removeidfobject(obj)
                    self.addidfobject(new_object)
                    log(
                        f"{obj} is a 'unique-object'; Removed and replaced with"
                        f" {new_object}",
                        lg.DEBUG,
                    )
                return new_object
            if new_object in existing_objs:
                # If obj already exists, simply return
                log(
                    f"object '{new_object}' already exists in {self.name}. "
                    f"Skipping.",
                    lg.DEBUG,
                )
                return new_object
            else:
                # add to model and return
                self.addidfobject(new_object)
                log(f"object '{new_object}' added to '{self.name}'", lg.DEBUG)
                return new_object

    def addidfobject(self, new_object):
        """Add an IDF object to the IDF.

        Args:
            new_object (EpBunch): The IDF object to copy.

        Returns:
            EpBunch: object.
        """
        key = new_object.key.upper()
        self.idfobjects[key].append(new_object)
        self._reset_dependant_vars("idfobjects")

    def removeidfobject(self, idfobject):
        """Remove an IDF object from the IDF.

        Parameters
        ----------
        idfobject : EpBunch object
            The IDF object to remove.

        """
        key = idfobject.key.upper()
        self.idfobjects[key].remove(idfobject)
        self._reset_dependant_vars("idfobjects")

    def anidfobject(self, key, aname="", **kwargs):
        # type: (str, str, **Any) -> EpBunch
        """Create an object, but don't add to the model (See
        :func:`~archetypal.idfclass.IDF.newidfobject`). If you don't specify a value
        for a field, the default value will be set.

        Example:
            >>> from archetypal import IDF
            >>> IDF.anidfobject("CONSTRUCTION")
            >>> IDF.anidfobject(
            >>>     key="CONSTRUCTION",
            >>>     Name='Interior Ceiling_class',
            >>>     Outside_Layer='LW Concrete',
            >>>     Layer_2='soundmat'
            >>> )

        Args:
            key (str): The type of IDF object. This must be in ALL_CAPS.
            aname (str): This parameter is not used. It is left there for backward
                compatibility.
            kwargs: Keyword arguments in the format `field=value` used to set
                fields in the EnergyPlus object.

        Returns:
            EpBunch: object.
        """
        obj = newrawobject(self.model, self.idd_info, key)
        abunch = obj2bunch(self.model, self.idd_info, obj)
        if aname:
            warnings.warn(
                "The aname parameter should no longer be used (%s)." % aname,
                UserWarning,
            )
            namebunch(abunch, aname)
        for k, v in kwargs.items():
            try:
                abunch[k] = v
            except BadEPFieldError as e:
                # Backwards compatibility
                if str(e) == "unknown field Key_Name":
                    abunch["Name"] = v
                else:
                    raise e
        return abunch

    def get_schedule_type_limits_data_by_name(self, schedule_limit_name):
        """Returns the data for a particular 'ScheduleTypeLimits' object

        Args:
            schedule_limit_name:
        """
        schedule = self.getobject("ScheduleTypeLimits".upper(), schedule_limit_name)

        if schedule is not None:
            lower_limit = schedule["Lower_Limit_Value"]
            upper_limit = schedule["Upper_Limit_Value"]
            numeric_type = schedule["Numeric_Type"]
            unit_type = schedule["Unit_Type"]

            if schedule["Unit_Type"] == "":
                unit_type = numeric_type

            return lower_limit, upper_limit, numeric_type, unit_type
        else:
            return "", "", "", ""

    def get_schedule_epbunch(self, name, sch_type=None):
        """Returns the epbunch of a particular schedule name. If the schedule
        type is known,retrievess it quicker.

        Args:
            name (str): The name of the schedule to retreive in the IDF file.
            sch_type (str): The schedule type, e.g.: "SCHEDULE:YEAR".
        """
        if sch_type is None:
            try:
                return self.schedules_dict[name.upper()]
            except KeyError:
                raise KeyError(
                    'Unable to find schedule "{}" of type "{}" '
                    'in idf file "{}"'.format(name, sch_type, self.name)
                )
        else:
            return self.getobject(sch_type.upper(), name)

    def get_all_schedules(self, yearly_only=False):
        """Returns all schedule ep_objects in a dict with their name as a key

        Args:
            yearly_only (bool): If True, return only yearly schedules

        Returns:
            (dict of eppy.bunch_subclass.EpBunch): the schedules with their
                name as a key
        """
        schedule_types = list(map(str.upper, self.getiddgroupdict()["Schedules"]))
        if yearly_only:
            schedule_types = [
                "Schedule:Year".upper(),
                "Schedule:Compact".upper(),
                "Schedule:Constant".upper(),
                "Schedule:File".upper(),
            ]
        scheds = {}
        for sched_type in schedule_types:
            for sched in self.idfobjects[sched_type]:
                try:
                    if sched.key.upper() in schedule_types:
                        scheds[sched.Name.upper()] = sched
                except KeyError:
                    pass
        return scheds

    def get_used_schedules(self, yearly_only=False):
        """Returns all used schedules

        Args:
            yearly_only (bool): If True, return only yearly schedules

        Returns:
            (list): the schedules names
        """
        schedule_types = [
            "Schedule:Day:Hourly".upper(),
            "Schedule:Day:Interval".upper(),
            "Schedule:Day:List".upper(),
            "Schedule:Week:Daily".upper(),
            "Schedule:Year".upper(),
            "Schedule:Week:Compact".upper(),
            "Schedule:Compact".upper(),
            "Schedule:Constant".upper(),
            "Schedule:File".upper(),
        ]

        used_schedules = []
        all_schedules = self.get_all_schedules(yearly_only=yearly_only)
        for object_name in self.idfobjects:
            for object in self.idfobjects[object_name]:
                if object.key.upper() not in schedule_types:
                    for fieldvalue in object.fieldvalues:
                        try:
                            if (
                                fieldvalue.upper() in all_schedules.keys()
                                and fieldvalue not in used_schedules
                            ):
                                used_schedules.append(fieldvalue)
                        except KeyError:
                            pass
        return used_schedules

    @deprecated(
        deprecated_in="1.3.5",
        removed_in="1.4",
        current_version=archetypal.__version__,
        details="Use IDF.name instead",
    )
    def building_name(self, use_idfname=False):
        """
        Args:
            use_idfname:
        """
        if use_idfname:
            return os.path.basename(self.idfname)
        else:
            bld = self.idfobjects["BUILDING"]
            if bld is not None:
                return bld[0].Name
            else:
                return os.path.basename(self.idfname)

    def rename(self, objkey, objname, newname):
        """rename all the references to this objname.

        Function comes from eppy.modeleditor and was modified to compare the
        name to rename with a lower string (see
        idfobject[idfobject.objls[findex]].lower() == objname.lower())

        Args:
            objkey (str): EpBunch we want to rename and rename all the
                occurrences where this object is in the IDF file
            objname (str): The name of the EpBunch to rename
            newname (str): New name used to rename the EpBunch

        Returns:
            theobject (EpBunch): The renamed idf object
        """

        refnames = eppy.modeleditor.getrefnames(self, objkey)
        for refname in refnames:
            objlists = eppy.modeleditor.getallobjlists(self, refname)
            # [('OBJKEY', refname, fieldindexlist), ...]
            for robjkey, refname, fieldindexlist in objlists:
                idfobjects = self.idfobjects[robjkey]
                for idfobject in idfobjects:
                    for findex in fieldindexlist:  # for each field
                        if (
                            idfobject[idfobject.objls[findex]].lower()
                            == objname.lower()
                        ):
                            idfobject[idfobject.objls[findex]] = newname
        theobject = self.getobject(objkey, objname)
        fieldname = [item for item in theobject.objls if item.endswith("Name")][0]
        theobject[fieldname] = newname
        return theobject

    def _set_cached_results(self, runargs, tmp, tmp_file):
        save_dir = self.simulation_dir
        if self.keep_data:
            save_dir.rmtree_p()  # purge target dir
            tmp.copytree(save_dir)  # copy files

            log(
                "Files generated at the end of the simulation: %s"
                % "\n".join(save_dir.files()),
                lg.DEBUG,
                name=self.name,
            )

            # save runargs
            # cache_runargs(tmp_file, runargs.copy())

    def _energy_series(
        self,
        energy_out_variable_name,
        units,
        name,
        prep_outputs=None,
        EnergySeries_kwds=None,
    ):
        """
        Args:
            energy_out_variable_name:
            units:
            name:
            prep_outputs (list):
            EnergySeries_kwds:
        """
        if prep_outputs:
            OutputPrep(self).add_custom(prep_outputs)
            self.simulate()
        rd = ReportData.from_sqlite(self.sql_file, table_name=energy_out_variable_name)
        profile = EnergySeries.from_sqlite(
            rd, to_units=units, name=name, **EnergySeries_kwds
        )
        return profile

    def _execute_transitions(self, idf_file, to_version, **kwargs):
        trans_exec = {
            parse(re.search(r"to-V(([\d])-([\d])-([\d]))", exec).group(1)): exec
            for exec in self.idfversionupdater_dir.files("Transition-V*")
        }

        transitions = [
            key for key in trans_exec if to_version >= key > self.idf_version
        ]
        transitions.sort()

        for trans in tqdm(
            transitions,
            position=self.position,
            desc=f"transition file #{self.position}-{self.name}",
        ):
            if not trans_exec[trans].exists():
                raise EnergyPlusProcessError(
                    cmd=trans_exec[trans],
                    stderr="The specified EnergyPlus version (v{}) does not have"
                    " the required transition program '{}' in the "
                    "PreProcess folder. See the documentation "
                    "(archetypal.readthedocs.io/troubleshooting.html#missing"
                    "-transition-programs) "
                    "to solve this issue".format(to_version, trans_exec[trans]),
                    idf=self.name,
                )
            else:
                cmd = [trans_exec[trans], idf_file]
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        cwd=self.idfversionupdater_dir,
                    )
                    process_output, error_output = process.communicate()
                    log(process_output.decode("utf-8"), lg.DEBUG)
                except CalledProcessError as exception:
                    log(
                        "{} failed with error\n".format(
                            idf_version_updater.__name__, str(exception)
                        ),
                        lg.ERROR,
                    )


@extend_class(EpBunch)
def __eq__(self, other):
    return all(str(a).upper() == str(b).upper() for a, b in zip(self.obj, other.obj))


@extend_class(EpBunch)
def get_default(self, name):
    if "default" in self.getfieldidd(name).keys():
        _type = _parse_idd_type(self, name)
        default_ = next(iter(self.getfieldidd_item(name, "default")), None)
        return _type(default_)
    else:
        return ""


@extend_class(Eplusdata)
def makedict(self, dictfile, fnamefobject):
    """stuff file data into the blank dictionary"""
    # fname = './exapmlefiles/5ZoneDD.idf'
    # fname = './1ZoneUncontrolled.idf'
    if isinstance(dictfile, Idd):
        localidd = copy.deepcopy(dictfile)
        dt, dtls = localidd.dt, localidd.dtls
    else:
        dt, dtls = self.initdict(dictfile)
    # astr = mylib2.readfile(fname)
    astr = fnamefobject.read()
    try:
        astr = astr.decode("ISO-8859-2")
    except AttributeError:
        pass
    fnamefobject.seek(0)
    nocom = removecomment(astr, "!")
    idfst = nocom
    # alist = string.split(idfst, ';')
    alist = idfst.split(";")
    lss = []
    for element in alist:
        # lst = string.split(element, ',')
        lst = element.split(",")
        lss.append(lst)

    for i in range(0, len(lss)):
        for j in range(0, len(lss[i])):
            lss[i][j] = lss[i][j].strip()

    for element in lss:
        node = element[0].upper()
        if node in dt:
            # stuff data in this key
            dt[node.upper()].append(element)
        else:
            # scream
            if node == "":
                continue
            log("this node -%s-is not present in base dictionary" % node)

    self.dt, self.dtls = dt, dtls
    return dt, dtls


def _parse_idd_type(epbunch, name):
    """parse the fieldvalue type into a python type. eg.: 'real' returns
    'float'.

    Possible types are:
        - integer -> int
        - real -> float
        - alpha -> str          (arbitrary string),
        - choice -> str         (alpha with specific list of choices, see \key)
        - object-list -> str    (link to a list of objects defined elsewhere, see \object-list and \reference)
        - external-list -> str  (uses a special list from an external source, see \external-list)
        - node -> str           (name used in connecting HVAC components)
    """
    _type = next(iter(epbunch.getfieldidd_item(name, "type")), "").lower()
    if _type == "real":
        return float
    elif _type == "alpha":
        return str
    elif _type == "integer":
        return int
    else:
        return str


@deprecated(
    deprecated_in="1.3.5",
    removed_in="1.4",
    current_version=archetypal.__version__,
    details="Use :class:`IDF` instead",
)
def load_idf(
    eplus_file,
    idd_filename=None,
    output_folder=None,
    include=None,
    weather_file=None,
    ep_version=None,
):
    """Returns a parsed IDF object from file. If *archetypal.settings.use_cache*
    is true, then the idf object is loaded from cache.

    Args:
        eplus_file (str): Either the absolute or relative path to the idf file.
        idd_filename (str, optional): Either the absolute or relative path to
            the EnergyPlus IDD file. If None, the function tries to find it at
            the default EnergyPlus install location.
        output_folder (Path, optional): Either the absolute or relative path of
            the output folder. Specify if the cache location is different than
            archetypal.settings.cache_folder.
        include (str, optional): List input files that need to be copied to the
            simulation directory. Those can be, for example, schedule files read
            by the idf file. If a string is provided, it should be in a glob
            form (see pathlib.Path.glob).
        weather_file: Either the absolute or relative path to the weather epw
            file.
        ep_version (str, optional): EnergyPlus version number to use, eg.:
            "9-2-0". Defaults to `settings.as_version` .

    Returns:
        IDF: The IDF object.
    """
    eplus_file = Path(eplus_file)
    start_time = time.time()

    idf = load_idf_object_from_cache(eplus_file)
    if idf:
        return idf
    else:
        # Else, run eppy to load the idf objects
        idf = _eppy_load(
            eplus_file,
            idd_filename,
            output_folder=output_folder,
            include=include,
            epw=weather_file,
            ep_version=ep_version if ep_version is not None else settings.ep_version,
        )
        log(
            'Loaded "{}" in {:,.2f} seconds\n'.format(
                eplus_file.basename(), time.time() - start_time
            )
        )
        return idf


def _eppy_load(
    file, idd_filename, output_folder=None, include=None, epw=None, ep_version=None
):
    """Uses package eppy to parse an idf file. Will also try to upgrade the idf
    file using the EnergyPlus Transition executables if the version of
    EnergyPlus is not installed on the machine.

    Args:
        file (str): path of the idf file.
        idd_filename: path of the EnergyPlus IDD file.
        output_folder (str): path to the output folder. Will default to the
            settings.cache_folder.
        include (str, optional): List input files that need to be copied to the
            simulation directory.if a string is provided, it should be in a glob
            form (see pathlib.Path.glob).
        epw (str, optional): path of the epw weather file.
        ep_version (str): EnergyPlus version number to use.

    Returns:
        eppy.modeleditor.IDF: IDF object
    """
    file = Path(file)
    cache_filename = hash_model(file)

    try:
        # first copy the file
        if not output_folder:
            output_folder = settings.cache_folder / cache_filename
        else:
            output_folder = Path(output_folder)

        output_folder.makedirs_p()
        if file.basename() not in [
            file.basename() for file in output_folder.glob("*.idf")
        ]:
            # The file does not exist; copy it to the output_folder & override path name
            file = Path(file.copy(output_folder))
        else:
            # The file already exists at the location. Use that file
            file = output_folder / file.basename()

        # Determine version of idf file by reading the text file
        if idd_filename is None:
            idd_filename = getiddfile(get_idf_version(file))

        # Initiate an eppy.modeleditor.IDF object
        IDF.setiddname(idd_filename, testing=True)
        # load the idf object
        idf_object = IDF(file, epw=epw)
        # Check version of IDF file against version of IDD file
        idf_version = idf_object.idfobjects["VERSION"][0].Version_Identifier
        idd_version = "{}.{}".format(
            idf_object.idd_version[0], idf_object.idd_version[1]
        )
    except FileNotFoundError:
        # Loading the idf object will raise a FileNotFoundError if the
        # version of EnergyPlus is not installed
        log("Transitioning idf file {}".format(file))
        # if they don't fit, upgrade file
        file = idf_version_updater(file, out_dir=output_folder, to_version=ep_version)
        idd_filename = getiddfile(get_idf_version(file))
        # Initiate an eppy.modeleditor.IDF object
        IDF.setiddname(idd_filename, testing=True)
        # load the idf object
        idf_object = IDF(file, epw=epw)
    else:
        # the versions fit, great!
        log(
            'The version of the IDF file "{}", version "{}", matched the '
            'version of EnergyPlus {}, version "{}", used to parse it.'.format(
                file.basename(), idf_version, idf_object.getiddname(), idd_version
            ),
            level=lg.DEBUG,
        )
    # when parsing is complete, save it to disk, then return object
    save_idf_object_to_cache(idf_object, idf_object.idfname, output_folder)
    if isinstance(include, str):
        include = Path().abspath().glob(include)
    if include is not None:
        [Path(file).copy(output_folder) for file in include]
    return idf_object


def save_idf_object_to_cache(idf_object, idf_file, output_folder=None, how=None):
    """Saves the object to disk. Essentially uses the pickling functions of
    python.

    Todo:
        * Json dump does not work yet.

    Args:
        idf_object (eppy.modeleditor.IDF): an eppy IDF object
        idf_file (str): file path of idf file
        output_folder (Path): temporary output directory (default:
            settings.cache_folder)
        how (str, optional): How the pickling is done. Choices are 'json' or
            'pickle'. json dump does not quite work yet. 'pickle' will save to a
            gzip'ed file instead of a regular binary file (.dat).

    Returns:
        None
    """
    # upper() can't take NoneType as input.
    if how is None:
        how = ""
    # The main function
    if settings.use_cache:
        if output_folder is None:
            output_folder = hash_model(idf_file)
            cache_dir = os.path.join(settings.cache_folder, output_folder)
        cache_dir = output_folder

        # create the folder on the disk if it doesn't already exist
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        if how.upper() == "JSON":
            cache_fullpath_filename = cache_dir / cache_dir.basename() + "idfs.json"
            import gzip, json

            with open(cache_fullpath_filename, "w") as file_handle:
                json.dump(
                    {
                        key: value.__dict__
                        for key, value in idf_object.idfobjects.items()
                    },
                    file_handle,
                    sort_keys=True,
                    indent=4,
                    check_circular=True,
                )

        elif how.upper() == "PICKLE":
            # create pickle and dump
            cache_fullpath_filename = cache_dir / cache_dir.basename() + "idfs.gzip"
            import gzip

            try:
                import cPickle as pickle
            except ImportError:
                import pickle
            start_time = time.time()
            with gzip.GzipFile(cache_fullpath_filename, "wb") as file_handle:
                pickle.dump(idf_object, file_handle, protocol=0)
            log(
                "Saved pickle to file in {:,.2f} seconds".format(
                    time.time() - start_time
                )
            )

        else:
            cache_fullpath_filename = cache_dir / cache_dir.basename() + "idfs.dat"
            try:
                import cPickle as pickle
            except ImportError:
                import pickle
            start_time = time.time()
            with open(cache_fullpath_filename, "wb") as file_handle:
                pickle.dump(idf_object, file_handle, protocol=-1)
            log(
                "Saved pickle to file in {:,.2f} seconds".format(
                    time.time() - start_time
                )
            )


def load_idf_object_from_cache(idf_file, how=None):
    """Load an idf instance from cache.

    Args:
        idf_file (str): Either the absolute or relative path to the idf file.
        how (str, optional): How the pickling is done. Choices are 'json' or
            'pickle' or 'idf'. json dump doesn't quite work yet. 'pickle' will
            load from a gzip'ed file instead of a regular binary file (.gzip).
            'idf' will load from idf file saved in cache (.dat).

    Returns:
        IDF: The IDF object.
    """
    # upper() can't take NoneType as input.
    if how is None:
        how = ""
    # The main function
    if settings.use_cache:
        cache_filename = hash_model(idf_file)
        if how.upper() == "JSON":
            cache_fullpath_filename = os.path.join(
                settings.cache_folder,
                cache_filename,
                os.extsep.join([cache_filename + "idfs", "json"]),
            )
            import json

            try:
                import cPickle as pickle
            except ImportError:
                import pickle
            start_time = time.time()
            if os.path.isfile(cache_fullpath_filename):
                if os.path.getsize(cache_fullpath_filename) > 0:
                    with open(cache_fullpath_filename, "rb") as file_handle:
                        idf = json.load(file_handle)
                    log(
                        'Loaded "{}" from pickled file in {:,.2f} seconds'.format(
                            os.path.basename(idf_file), time.time() - start_time
                        )
                    )
                    return idf

        elif how.upper() == "PICKLE":
            cache_fullpath_filename = os.path.join(
                settings.cache_folder,
                cache_filename,
                os.extsep.join([cache_filename + "idfs", "gzip"]),
            )
            import gzip

            try:
                import cPickle as pickle
            except ImportError:
                import pickle
            start_time = time.time()
            if os.path.isfile(cache_fullpath_filename):
                if os.path.getsize(cache_fullpath_filename) > 0:
                    with gzip.GzipFile(cache_fullpath_filename, "rb") as file_handle:
                        try:
                            idf = pickle.load(file_handle)
                        except EOFError:
                            return None
                    if idf.iddname is None:
                        idf.setiddname(getiddfile(idf.model.dt["VERSION"][0][1]))
                        # idf.read()
                    log(
                        'Loaded "{}" from pickled file in {:,.2f} seconds'.format(
                            os.path.basename(idf_file), time.time() - start_time
                        )
                    )
                    return idf
        elif how.upper() == "IDF":
            cache_fullpath_filename = os.path.join(
                settings.cache_folder,
                cache_filename,
                os.extsep.join([cache_filename, "idf"]),
            )
            if os.path.isfile(cache_fullpath_filename):
                version = get_idf_version(cache_fullpath_filename, doted=True)
                iddfilename = getiddfile(version)
                idf = _eppy_load(cache_fullpath_filename, iddfilename)
                return idf
        else:
            cache_fullpath_filename = os.path.join(
                settings.cache_folder,
                cache_filename,
                os.extsep.join([cache_filename + "idfs", "dat"]),
            )
            try:
                import cPickle as pickle
            except ImportError:
                import pickle
            start_time = time.time()
            if os.path.isfile(cache_fullpath_filename):
                if os.path.getsize(cache_fullpath_filename) > 0:
                    with open(cache_fullpath_filename, "rb") as file_handle:
                        try:
                            idf = pickle.load(file_handle)
                        except EOFError:
                            return None
                    if idf.iddname is None:
                        idf.setiddname(getiddfile(idf.model.dt["VERSION"][0][1]))
                        idf.read()
                    log(
                        'Loaded "{}" from pickled file in {:,.2f} seconds'.format(
                            os.path.basename(idf_file), time.time() - start_time
                        )
                    )
                    return idf


class Outputs:
    """Handles preparation of EnergyPlus outputs. Different instance methods
    allow to chain methods together and to add predefined bundles of outputs in
    one go.

    Examples:
        >>> from archetypal import IDF
        >>> idf = IDF(prep_outputs=False)  # True be default
        >>> idf.outputs.add_output_control().add_umi_ouputs(
        >>> ).add_profile_gas_elect_ouputs().apply()
    """

    def __init__(self, idf):
        """Initialize an outputs object.

        Args:
            idf (IDF): the IDF object for wich this outputs object is created.
        """
        self.idf = idf
        self._outputs = []

    def add_custom(self, outputs):
        """Add custom-defined outputs as a list of objects.

        Examples:
            >>> outputs = IDF().outputs
            >>> to_add = dict(
            >>>       key= "OUTPUT:METER",
            >>>       Key_Name="Electricity:Facility",
            >>>       Reporting_Frequency="hourly",
            >>> )
            >>> outputs.add_custom([to_add]).apply()

        Args:
            outputs (list, bool): Pass a list of ep-objects defined as dictionary. See
                examples. If a bool, ignored.
        """
        if isinstance(outputs, list):
            self._outputs.extend(outputs)
        return self

    def add_basics(self):
        """Adds the summary report and the sql file to the idf outputs"""
        return self.add_summary_report().add_output_control().add_sql().add_schedules()

    def add_schedules(self):
        """Adds Schedules object"""
        outputs = [{"key": "Output:Schedules".upper(), **dict(Key_Field="Hourly")}]

        self._outputs.extend(outputs)
        return self

    def add_summary_report(self, summary="AllSummary"):
        """Adds the Output:Table:SummaryReports object.

        Args:
            summary (str): Choices are AllSummary, AllMonthly,
                AllSummaryAndMonthly, AllSummaryAndSizingPeriod,
                AllSummaryMonthlyAndSizingPeriod,
                AnnualBuildingUtilityPerformanceSummary,
                InputVerificationandResultsSummary,
                SourceEnergyEndUseComponentsSummary, ClimaticDataSummary,
                EnvelopeSummary, SurfaceShadowingSummary, ShadingSummary,
                LightingSummary, EquipmentSummary, HVACSizingSummary,
                ComponentSizingSummary, CoilSizingDetails, OutdoorAirSummary,
                SystemSummary, AdaptiveComfortSummary, SensibleHeatGainSummary,
                Standard62.1Summary, EnergyMeters, InitializationSummary,
                LEEDSummary, TariffReport, EconomicResultSummary,
                ComponentCostEconomicsSummary, LifeCycleCostReport,
                HeatEmissionsSummary,
        """
        outputs = [
            {
                "key": "Output:Table:SummaryReports".upper(),
                **dict(Report_1_Name=summary),
            }
        ]

        self._outputs.extend(outputs)
        return self

    def add_sql(self, sql_output_style="SimpleAndTabular"):
        """Adds the `Output:SQLite` object. This object will produce an sql file
        that contains the simulation results in a database format. See
        `eplusout.sql
        <https://bigladdersoftware.com/epx/docs/9-2/output-details-and
        -examples/eplusout-sql.html#eplusout.sql>`_ for more details.

        Args:
            sql_output_style (str): The *Simple* option will include all of the
                predefined database tables as well as time series related data.
                Using the *SimpleAndTabular* choice adds database tables related
                to the tabular reports that are already output by EnergyPlus in
                other formats.
        """
        output = {
            "key": "Output:SQLite".upper(),
            **dict(Option_Type=sql_output_style),
        }

        self._outputs.extend([output])
        return self

    def add_output_control(self, output_control_table_style="CommaAndHTML"):
        """Sets the `OutputControl:Table:Style` object.

        Args:
            output_control_table_style (str): Choices are: Comma, Tab, Fixed,
                HTML, XML, CommaAndHTML, TabAndHTML, XMLAndHTML, All
        """
        outputs = [
            {
                "key": "OutputControl:Table:Style".upper(),
                **dict(Column_Separator=output_control_table_style),
            }
        ]

        self._outputs.extend(outputs)
        return self

    def add_umi_template_outputs(self):
        """Adds the necessary outputs in order to create an UMI template."""
        # list the outputs here
        outputs = [
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Air System Total Heating Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Air System Total Cooling Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Zone Ideal Loads Zone Total Cooling Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Zone Ideal Loads Zone Total Heating Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Zone Thermostat Heating Setpoint Temperature",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Zone Thermostat Cooling Setpoint Temperature",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Heat Exchanger Total Heating Rate",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Heat Exchanger Sensible Effectiveness",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Heat Exchanger Latent Effectiveness",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Water Heater Heating Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "OUTPUT:METER",
                **dict(
                    Key_Name="HeatRejection:EnergyTransfer",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Heating:EnergyTransfer", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Cooling:EnergyTransfer", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(
                    Key_Name="Heating:DistrictHeating", Reporting_Frequency="hourly"
                ),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Heating:Electricity", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Heating:Gas", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(
                    Key_Name="Cooling:DistrictCooling", Reporting_Frequency="hourly"
                ),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Cooling:Electricity", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Cooling:Electricity", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Cooling:Gas", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(
                    Key_Name="WaterSystems:EnergyTransfer", Reporting_Frequency="hourly"
                ),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Cooling:Gas", Reporting_Frequency="hourly"),
            },
        ]

        self._outputs.extend(outputs)
        return self

    def add_umi_ouputs(self):
        """Adds the necessary outputs in order to return the same energy profile
        as in UMI.
        """
        # list the outputs here
        outputs = [
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Air System Total Heating Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Air System Total Cooling Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Zone Ideal Loads Zone Total Cooling Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Zone Ideal Loads Zone Total Heating Energy",
                    Reporting_Frequency="hourly",
                ),
            },
            {
                "key": "Output:Variable".upper(),
                **dict(
                    Variable_Name="Water Heater Heating Energy",
                    Reporting_Frequency="hourly",
                ),
            },
        ]

        self._outputs.extend(outputs)
        return self

    def add_profile_gas_elect_ouputs(self):
        """Adds the following meters: Electricity:Facility, Gas:Facility,
        WaterSystems:Electricity, Heating:Electricity, Cooling:Electricity
        """
        # list the outputs here
        outputs = [
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Electricity:Facility", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Gas:Facility", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(
                    Key_Name="WaterSystems:Electricity", Reporting_Frequency="hourly"
                ),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Heating:Electricity", Reporting_Frequency="hourly"),
            },
            {
                "key": "OUTPUT:METER",
                **dict(Key_Name="Cooling:Electricity", Reporting_Frequency="hourly"),
            },
        ]
        self._outputs.extend(outputs)
        return self

    def apply(self):
        """Applies the outputs to the idf model. Modifies the model bu calling
        :meth:`~archetypal.idfclass.IDF.newidfobject`"""
        for output in self._outputs:
            self.idf.newidfobject(**output)
        return self


@deprecated(
    deprecated_in="1.4",
    removed_in="1.5",
    current_version=archetypal.__version__,
    details="use IDF.outputs instead",
)
class OutputPrep(Outputs):
    pass


def cache_runargs(eplus_file, runargs):
    """
    Args:
        eplus_file:
        runargs:
    """
    import json

    output_directory = runargs["output_directory"] / runargs["output_prefix"]

    runargs.update({"run_time": datetime.datetime.now().isoformat()})
    runargs.update({"idf_file": eplus_file})
    with open(os.path.join(output_directory, "runargs.json"), "w") as fp:
        json.dump(runargs, fp, sort_keys=True, indent=4)


@deprecated(
    deprecated_in="1.3.5",
    removed_in="1.4",
    current_version=archetypal.__version__,
    details="Use IDF.simulate() instead",
)
def run_eplus(
    eplus_file,
    weather_file,
    output_directory=None,
    ep_version=None,
    output_report=None,
    prep_outputs=False,
    simulname=None,
    keep_data=True,
    annual=False,
    design_day=False,
    epmacro=False,
    expandobjects=True,
    readvars=False,
    output_prefix=None,
    output_suffix=None,
    version=None,
    verbose="v",
    keep_data_err=False,
    include=None,
    process_files=False,
    custom_processes=None,
    return_idf=False,
    return_files=False,
    **kwargs,
):
    """Run an EnergyPlus file using the EnergyPlus executable.

    Specify run options:
        Run options are specified in the same way as the E+ command line
        interface: annual, design_day, epmacro, expandobjects, etc. are all
        supported.

    Specify outputs:
        Optionally define the desired outputs by specifying the
        :attr:`prep_outputs` attribute.

        With the :attr:`prep_outputs` attribute, specify additional outputs
        objects to append to the energy plus file. If True is specified, a selection of
        useful options will be append by default (see: :class:`OutputPrep`
        for more details).

    Args:
        eplus_file (str): path to the idf file.
        weather_file (str): path to the EPW weather file.
        output_directory (str, optional): path to the output folder. Will
            default to the settings.cache_folder.
        ep_version (str, optional): EnergyPlus version to use, eg: 9-2-0
        output_report: 'sql' or 'htm'.
        prep_outputs (bool or list, optional): if True, meters and variable
            outputs will be appended to the idf files. Can also specify custom
            outputs as list of ep-object outputs.
        simulname (str): The name of the simulation. (Todo: Currently not implemented).
        keep_data (bool): If True, files created by EnergyPlus are saved to the
            output_directory.
        annual (bool): If True then force annual simulation (default: False)
        design_day (bool): Force design-day-only simulation (default: False)
        epmacro (bool): Run EPMacro prior to simulation (default: False)
        expandobjects (bool): Run ExpandObjects prior to simulation (default:
            True)
        readvars (bool): Run ReadVarsESO after simulation (default: False)
        output_prefix (str, optional): Prefix for output file names.
        output_suffix (str, optional): Suffix style for output file names
            (default: L) Choices are:
                - L: Legacy (e.g., eplustbl.csv)
                - C: Capital (e.g., eplusTable.csv)
                - D: Dash (e.g., eplus-table.csv)
        version (bool, optional): Display version information (default: False)
        verbose (str): Set verbosity of runtime messages (default: v) v: verbose
            q: quiet
        keep_data_err (bool): If True, errored directory where simulation occurred is
            kept.
        include (str, optional): List input files that need to be copied to the
            simulation directory. If a string is provided, it should be in a glob
            form (see :meth:`pathlib.Path.glob`).
        process_files (bool): If True, process the output files and load to a
            :class:`~pandas.DataFrame`. Custom processes can be passed using the
            :attr:`custom_processes` attribute.
        custom_processes (dict(Callback)): if provided, it has to be a
            dictionary with the keys being a glob (see :meth:`pathlib.Path.glob`), and
            the value a Callback taking as signature `callback(file: str,
            working_dir, simulname) -> Any` All the file matching this glob will
            be processed by this callback. Note: they will still be processed by
            pandas.read_csv (if they are csv files), resulting in duplicate. The
            only way to bypass this behavior is to add the key "*.csv" to that
            dictionary.
        return_idf (bool): If True, returns the :class:`IDF` object part of the
            return tuple.
        return_files (bool): It True, all files paths created by the EnergyPlus
            run are returned.

    Returns:
        2-tuple: a 1-tuple or a 2-tuple
            - dict: dict of [(title, table), .....]
            - IDF: The IDF object. Only provided if return_idf is True.

    Raises:
        EnergyPlusProcessError.
    """
    eplus_file = Path(eplus_file)
    weather_file = Path(weather_file)

    frame = inspect.currentframe()
    args, _, _, values = inspect.getargvalues(frame)
    args = {arg: values[arg] for arg in args}

    cache_filename = hash_model(eplus_file)
    if not output_prefix:
        output_prefix = cache_filename
    if not output_directory:
        output_directory = settings.cache_folder / cache_filename
    else:
        output_directory = Path(output_directory)
    args["output_directory"] = output_directory
    # <editor-fold desc="Try to get cached results">
    try:
        start_time = time.time()
        cached_run_results = get_from_cache(args)
    except Exception as e:
        # catch other exceptions that could occur
        raise Exception("{}".format(e))
    else:
        if cached_run_results:
            # if cached run found, simply return it
            log(
                "Successfully parsed cached idf run in {:,.2f} seconds".format(
                    time.time() - start_time
                ),
                name=eplus_file.basename(),
            )
            # return_idf
            if return_idf:
                filepath = os.path.join(
                    output_directory,
                    hash_model(output_directory / eplus_file.basename()),
                    eplus_file.basename(),
                )
                idf = load_idf(
                    filepath,
                    weather_file=weather_file,
                    output_folder=output_directory,
                    include=include,
                )
            else:
                idf = None
            if return_files:
                files = Path(
                    os.path.join(
                        output_directory,
                        hash_model(output_directory / eplus_file.basename()),
                    )
                ).files()
            else:
                files = None
            return_elements = list(
                compress(
                    [cached_run_results, idf, files], [True, return_idf, return_files]
                )
            )
            return _unpack_tuple(return_elements)

    runs_not_found = eplus_file
    # </editor-fold>

    # <editor-fold desc="Upgrade the file version if needed">
    if ep_version:
        # if users specifies version, make sure dots are replaced with "-".
        ep_version = ep_version.replace(".", "-")
    else:
        # if no version is specified, take the package default version
        ep_version = archetypal.settings.ep_version
    eplus_file = idf_version_updater(
        upgraded_file(eplus_file, output_directory),
        to_version=ep_version,
        out_dir=output_directory,
    )
    # In case the file has been updated, update the versionid of the file
    # and the idd_file
    versionid = get_idf_version(eplus_file, doted=False)
    idd_file = Path(getiddfile(get_idf_version(eplus_file, doted=True)))
    # </editor-fold>

    # Prepare outputs e.g. sql table
    if prep_outputs:
        # Check if idf file has necessary objects (eg specific outputs)
        idf_obj = load_idf(
            eplus_file,
            idd_filename=idd_file,
            output_folder=output_directory,
            weather_file=weather_file,
        )
        # Call the OutputPrep class with chained instance methods to add all
        # necessary outputs + custom ones defined in the parameters of this function.
        OutputPrep(idf=idf_obj).add_basics().add_umi_template_outputs.add_custom(
            outputs=prep_outputs
        ).add_profile_gas_elect_ouputs()

    if runs_not_found:
        # continue with simulation of other files
        log(
            "no cached run for {}. Running EnergyPlus...".format(
                os.path.basename(eplus_file)
            ),
            name=eplus_file.basename(),
        )

        start_time = time.time()
        if isinstance(include, str):
            include = Path().abspath().glob(include)
        elif include is not None:
            include = [Path(file) for file in include]
        # run the EnergyPlus Simulation
        with TempDir(
            prefix="eplus_run_", suffix=output_prefix, dir=output_directory
        ) as tmp:
            log(
                f"temporary dir ({Path(tmp).expand()}) created",
                lg.DEBUG,
                name=eplus_file.basename(),
            )
            if include:
                include = [file.copy(tmp) for file in include]
            tmp_file = Path(eplus_file.copy(tmp))
            runargs = {
                "tmp": tmp,
                "eplus_file": tmp_file,
                "weather": Path(weather_file.copy(tmp)),
                "verbose": verbose,
                "output_directory": output_directory,
                "as_version": versionid,
                "output_prefix": hash_model(eplus_file),
                "idd": Path(idd_file.copy(tmp)),
                "annual": annual,
                "epmacro": epmacro,
                "readvars": readvars,
                "output_suffix": output_suffix,
                "version": version,
                "expandobjects": expandobjects,
                "design_day": design_day,
                "keep_data_err": keep_data_err,
                "output_report": output_report,
                "include": include,
                "custom_processes": custom_processes,
            }

            _run_exec(**runargs)

            log(
                "EnergyPlus Completed in {:,.2f} seconds".format(
                    time.time() - start_time
                ),
                name=eplus_file.basename(),
            )

            processes = {"*.csv": _process_csv}  # output_prefix +
            if custom_processes is not None:
                processes.update(custom_processes)

            results = []
            if process_files:

                for glob, process in processes.items():
                    results.extend(
                        [
                            (
                                file.basename(),
                                process(
                                    file,
                                    working_dir=os.getcwd(),
                                    simulname=output_prefix,
                                ),
                            )
                            for file in tmp.files(glob)
                        ]
                    )

            save_dir = output_directory / hash_model(eplus_file)
            if keep_data:
                save_dir.rmtree_p()
                tmp.copytree(save_dir)

                log(
                    "Files generated at the end of the simulation: %s"
                    % "\n".join(save_dir.files()),
                    lg.DEBUG,
                    name=eplus_file.basename(),
                )
                if return_files:
                    files = save_dir.files()
                else:
                    files = None

                # save runargs
                cache_runargs(tmp_file, runargs.copy())

            # Return summary DataFrames
            runargs["output_directory"] = save_dir
            cached_run_results = get_report(**runargs)
            if return_idf:
                idf = load_idf(
                    eplus_file,
                    output_folder=output_directory,
                    include=include,
                    weather_file=weather_file,
                )
                runargs["output_report"] = "sql"
                idf._sql = get_report(**runargs)
                runargs["output_report"] = "sql_file"
                idf._sql_file = get_report(**runargs)
                runargs["output_report"] = "htm"
                idf._htm = get_report(**runargs)
            else:
                idf = None
            return_elements = list(
                compress(
                    [cached_run_results, idf, files], [True, return_idf, return_files]
                )
            )
        return _unpack_tuple(return_elements)


def upgraded_file(eplus_file, output_directory):
    """returns the eplus_file path that would have been copied in the output
    directory if it exists

    Args:
        eplus_file:
        output_directory:
    """
    if settings.use_cache:
        eplus_file = next(iter(output_directory.glob("*.idf")), eplus_file)
    return eplus_file


def _process_csv(file, working_dir, simulname):
    """
    Args:
        file:
        working_dir:
        simulname:
    """
    log("looking for csv output, return the csv files in DataFrames if any")
    if "table" in file.basename():
        tables_out = working_dir.abspath() / "tables"
        tables_out.makedirs_p()
        file.copy(tables_out / "%s_%s.csv" % (file.basename().stripext(), simulname))
        return
    log("try to store file %s in DataFrame" % file)
    try:
        df = pd.read_csv(file, sep=",", encoding="us-ascii")
    except ParserError:
        pass
    else:
        log("file %s stored" % file)
        return df


def _run_exec(
    tmp,
    eplus_file,
    weather,
    output_directory,
    annual,
    design_day,
    idd,
    epmacro,
    expandobjects,
    readvars,
    output_prefix,
    output_suffix,
    version,
    verbose,
    ep_version,
    keep_data_err,
    include,
    custom_processes,
    **kwargs,
):
    """Wrapper around the EnergyPlus command line interface.

    Adapted from :func:`eppy.runner.runfunctions.run`.

    Args:
        tmp:
        eplus_file:
        weather:
        output_directory:
        annual:
        design_day:
        idd:
        epmacro:
        expandobjects:
        readvars:
        output_prefix:
        output_suffix:
        version:
        verbose:
        ep_version:
        keep_data_err:
        include:
    """

    args = locals().copy()
    kwargs = args.pop("kwargs")
    # get unneeded params out of args ready to pass the rest to energyplus.exe
    verbose = args.pop("verbose")
    eplus_file = args.pop("eplus_file")
    iddname = args.get("idd")
    tmp = args.pop("tmp")
    keep_data_err = args.pop("keep_data_err")
    output_directory = args.pop("output_directory")
    idd = args.pop("idd")
    include = args.pop("include")
    custom_processes = args.pop("custom_processes")
    try:
        idf_path = os.path.abspath(eplus_file.idfname)
    except AttributeError:
        idf_path = os.path.abspath(eplus_file)
    ep_version = args.pop("ep_version")
    # get version from IDF object or by parsing the IDF file for it
    if not ep_version:
        try:
            ep_version = "-".join(str(x) for x in eplus_file.idd_version[:3])
        except AttributeError:
            raise AttributeError(
                "The as_version must be set when passing an IDF path. \
                Alternatively, use IDF.run()"
            )

    eplus_exe_path, eplus_weather_path = eppy.runner.run_functions.install_paths(
        ep_version, iddname
    )
    if version:
        # just get EnergyPlus version number and return
        cmd = [eplus_exe_path, "--version"]
        subprocess.check_call(cmd)
        return

    # convert paths to absolute paths if required
    if os.path.isfile(args["weather"]):
        args["weather"] = os.path.abspath(args["weather"])
    else:
        args["weather"] = os.path.join(eplus_weather_path, args["weather"])
    # args['output_directory'] = tmp.abspath()

    with tmp.abspath() as tmp:
        # build a list of command line arguments
        cmd = [eplus_exe_path]
        for arg in args:
            if args[arg]:
                if isinstance(args[arg], bool):
                    args[arg] = ""
                cmd.extend(["--{}".format(arg.replace("_", "-"))])
                if args[arg] != "":
                    cmd.extend([args[arg]])
        cmd.extend([idf_path])
        position = kwargs.get("position", None)
        with tqdm(
            unit_scale=True, miniters=1, desc=f"run_eplus={position}", position=position
        ) as progress:
            with subprocess.Popen(
                cmd,
                shell=True,
                universal_newlines=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                _log_subprocess_output(
                    process.stdout,
                    name=eplus_file.basename(),
                    verbose=verbose,
                    progress=progress,
                )
                # We explicitly close stdout
                process.stdout.close()

                # wait for the return code
                return_code = process.wait()

                # if return code is not 0 this means our script errored out
                if return_code != 0:
                    error_filename = output_prefix + "out.err"
                    try:
                        with open(error_filename, "r") as stderr:
                            stderr_r = stderr.read()
                        if keep_data_err:
                            failed_dir = output_directory / "failed"
                            failed_dir.mkdir_p()
                            tmp.copytree(failed_dir / output_prefix)
                        raise EnergyPlusProcessError(
                            cmd=cmd, stderr=stderr_r, idf=eplus_file.basename(),
                        )
                    except FileNotFoundError:
                        raise CalledProcessError(
                            return_code, cmd=cmd, stderr=process.stderr
                        )


def _log_subprocess_output(pipe, name, verbose, progress):
    """
    Args:
        pipe:
        name:
        verbose:
        progress (tqdm): tqdm progress bar
    """
    logger = None
    for line in pipe:
        linetxt = line.strip("\n")
        if verbose == "v":
            logger = log(
                linetxt,
                level=lg.DEBUG,
                name="eplus_run_" + name,
                filename="eplus_run_" + name,
                log_dir=os.getcwd(),
            )

        if linetxt != "":
            progress.update()
    if logger:
        close_logger(logger)
    if pipe:
        sys.stdout.flush()


def hash_model(idfname, **kwargs):
    """Simple function to hash a file or IDF model and return it as a string. Will also
    hash the :func:`eppy.runner.run_functions.run()` arguments so that correct
    results are returned when different run arguments are used.

    Todo:
        Hashing should include the external files used an idf file. For example,
        if a model uses a csv file as an input and that file changes, the
        hashing will currently not pickup that change. This could result in
        loading old results without the user knowing.

    Args:
        idfname (str or IDF): path of the idf file or the IDF model itself.
        kwargs: keywargs to serialize in addition to the file content.

    Returns:
        str: The digest value as a string of hexadecimal digits
    """
    if kwargs:
        # Before we hash the kwargs, remove the ones that don't have an impact on
        # simulation results and so should not change the cache dirname.
        no_impact = ["keep_data", "keep_data_err", "return_idf", "return_files"]
        for argument in no_impact:
            _ = kwargs.pop(argument, None)

        # sorting keys for serialization of dictionary
        kwargs = OrderedDict(sorted(kwargs.items()))

    # create hasher
    hasher = hashlib.md5()
    if isinstance(idfname, StringIO):
        idfname.seek(0)
        buf = idfname.read().encode("utf-8")
    elif isinstance(idfname, IDF):
        buf = idfname.idfstr().encode("utf-8")
    else:
        with open(idfname, "rb") as afile:
            buf = afile.read()
    hasher.update(buf)
    hasher.update(kwargs.__str__().encode("utf-8"))  # Hashing the kwargs as well
    return hasher.hexdigest()


def get_report(
    eplus_file, output_directory=None, output_report="sql", output_prefix=None, **kwargs
):
    """Returns the specified report format (html or sql)

    Args:
        eplus_file (str): path of the idf file
        output_directory (str, optional): path to the output folder. Will
            default to the settings.cache_folder.
        output_report: 'htm' or 'sql'
        output_prefix (str): Prefix name given to results files.
        **kwargs: keyword arguments to pass to hasher.

    Returns:
        dict: a dict of DataFrames
    """
    if not output_directory:
        output_directory = settings.cache_folder
    # Hash the idf file with any kwargs used in the function
    if output_prefix is None:
        output_prefix = hash_model(eplus_file)
    if output_report is None:
        return None
    elif "htm" in output_report.lower():
        # Get the html report
        fullpath_filename = output_directory / output_prefix + "tbl.htm"
        if fullpath_filename.exists():
            return get_html_report(fullpath_filename)
        else:
            raise FileNotFoundError(
                'File "{}" does not exist'.format(fullpath_filename)
            )

    elif "sql" == output_report.lower():
        # Get the sql report
        fullpath_filename = output_directory / output_prefix + "out.sql"
        if fullpath_filename.exists():
            return get_sqlite_report(fullpath_filename)
        else:
            raise FileNotFoundError(
                'File "{}" does not exist'.format(fullpath_filename)
            )
    else:
        return None


def get_from_cache(kwargs):
    """Retrieve a EPlus Tabulated Summary run result from the cache

    Args:
        kwargs (dict): Args used to create the cache name.

    Returns:
        dict: dict of DataFrames
    """
    output_directory = Path(kwargs.get("output_directory"))
    output_report = kwargs.get("output_report")
    eplus_file = next(iter(output_directory.glob("*.idf")), None)
    if not eplus_file:
        return None
    if settings.use_cache:
        # determine the filename by hashing the eplus_file
        cache_filename_prefix = hash_model(eplus_file)

        if output_report is None:
            # No report is expected but we should still return the path if it exists.
            cached_run_dir = output_directory / cache_filename_prefix
            if cached_run_dir.exists():
                return cached_run_dir
            else:
                return None
        elif "htm" in output_report.lower():
            # Get the html report

            cache_fullpath_filename = (
                output_directory / cache_filename_prefix / cache_filename_prefix
                + "tbl.htm"
            )
            if cache_fullpath_filename.exists():
                return get_html_report(cache_fullpath_filename)

        elif "sql" == output_report.lower():
            # get the SQL report
            if not output_directory:
                output_directory = settings.cache_folder / cache_filename_prefix

            cache_fullpath_filename = (
                output_directory / cache_filename_prefix / cache_filename_prefix
                + "out.sql"
            )

            if cache_fullpath_filename.exists():
                # get reports from passed-in report names or from
                # settings.available_sqlite_tables if None are given
                return get_sqlite_report(
                    cache_fullpath_filename,
                    kwargs.get("report_tables", settings.available_sqlite_tables),
                )
        elif "sql_file" == output_report.lower():
            # get the SQL report
            if not output_directory:
                output_directory = settings.cache_folder / cache_filename_prefix

            cache_fullpath_filename = (
                output_directory / cache_filename_prefix / cache_filename_prefix
                + "out.sql"
            )
            if cache_fullpath_filename.exists():
                return cache_fullpath_filename


def get_html_report(report_fullpath):
    """Parses the html Summary Report for each tables into a dictionary of
    DataFrames

    Args:
        report_fullpath (str): full path to the report file

    Returns:
        dict: dict of {title : table <DataFrame>,...}
    """
    from eppy.results import readhtml  # the eppy module with functions to read the html

    with open(report_fullpath, "r", encoding="utf-8") as cache_file:
        filehandle = cache_file.read()  # get a file handle to the html file

        cached_tbl = readhtml.titletable(
            filehandle
        )  # get a file handle to the html file

        log('Retrieved response from cache file "{}"'.format(report_fullpath))
        return summary_reports_to_dataframes(cached_tbl)


def summary_reports_to_dataframes(reports_list):
    """Converts a list of [(title, table),...] to a dict of {title: table
    <DataFrame>}. Duplicate keys must have their own unique names in the output
    dict.

    Args:
        reports_list (list): a list of [(title, table),...]

    Returns:
        dict: a dict of {title: table <DataFrame>}
    """
    results_dict = {}
    for table in reports_list:
        key = str(table[0])
        if key in results_dict:  # Check if key is already exists in
            # dictionary and give it a new name
            key = key + "_"
        df = pd.DataFrame(table[1])
        df = df.rename(columns=df.iloc[0]).drop(df.index[0])
        results_dict[key] = df
    return results_dict


def get_sqlite_report(report_file, report_tables=None):
    """Connects to the EnergyPlus SQL output file and retreives all tables

    Args:
        report_file (str): path of report file
        report_tables (list, optional): list of report table names to retreive.
            Defaults to settings.available_sqlite_tables

    Returns:
        dict: dict of DataFrames
    """
    # set list of report tables
    if not report_tables:
        report_tables = settings.available_sqlite_tables

    # if file exists, parse it with pandas' read_sql_query
    if os.path.isfile(report_file):
        import sqlite3
        import numpy as np

        # create database connection with sqlite3
        with sqlite3.connect(report_file) as conn:
            # empty dict to hold all DataFrames
            all_tables = {}
            # Iterate over all tables in the report_tables list
            for table in report_tables:
                try:
                    # Try regular str read, could fail if wrong encoding
                    conn.text_factory = str
                    df = pd.read_sql_query(
                        "select * from {};".format(table),
                        conn,
                        index_col=report_tables[table]["PrimaryKey"],
                        parse_dates=report_tables[table]["ParseDates"],
                        coerce_float=True,
                    )
                    all_tables[table] = df
                except OperationalError:
                    # Wring encoding found, the load bytes and ecode object
                    # columns only
                    conn.text_factory = bytes
                    df = pd.read_sql_query(
                        "select * from {};".format(table),
                        conn,
                        index_col=report_tables[table]["PrimaryKey"],
                        parse_dates=report_tables[table]["ParseDates"],
                        coerce_float=True,
                    )
                    str_df = df.select_dtypes([np.object])
                    str_df = str_df.stack().str.decode("8859").unstack()
                    for col in str_df:
                        df[col] = str_df[col]
                    all_tables[table] = df
            log(
                "SQL query parsed {} tables as DataFrames from {}".format(
                    len(all_tables), report_file
                )
            )
            return all_tables


def idf_version_updater(
    idf_file, to_version=None, out_dir=None, simulname=None, overwrite=True, **kwargs
):
    """EnergyPlus idf version updater using local transition program.

    Update the EnergyPlus simulation file (.idf) to the latest available
    EnergyPlus version installed on this machine. Optionally specify a version
    (eg.: "9-2-0") to aim for a specific version. The output will be the path of
    the updated file. The run is multiprocessing_safe.

    Hint:
        If attempting to upgrade an earlier version of EnergyPlus ( pre-v7.2.0),
        specific binaries need to be downloaded and copied to the
        EnergyPlus*/PreProcess/IDFVersionUpdater folder. More info at
        `Converting older version files
        <http://energyplus.helpserve.com/Knowledgebase/List/Index/46
        /converting-older-version-files>`_ .

    Args:
        overwrite:
        idf_file (Path): path of idf file
        to_version (str, optional): EnergyPlus version in the form "X-X-X".
        out_dir (Path): path of the output_dir
        simulname (str or None, optional): this name will be used for temp dir
            id and saved outputs. If not provided, uuid.uuid1() is used. Be
            careful to avoid naming collision : the run will always be done in
            separated folders, but the output files can overwrite each other if
            the simulname is the same. (default: None)

    Raises:
        EnergyPlusProcessError: If version updater fails.
        EnergyPlusVersionError:
        CalledProcessError:

    Returns:
        Path: The path of the new transitioned idf file.
    """
    idf_file = Path(idf_file)
    if not out_dir:
        # if no directory is provided, use directory of file
        out_dir = idf_file.dirname()
    if not out_dir.isdir() and out_dir != "":
        # check if dir exists
        out_dir.makedirs_p()

    to_version, versionid = _check_version(idf_file, to_version, out_dir)

    if versionid == to_version:
        # check the file version, if it corresponds to the latest version found on
        # the machine, means its already upgraded to the correct version.
        # if file version and to_version are the same, we don't need to
        # perform transition.
        log(
            'file {} already upgraded to latest version "{}"'.format(
                idf_file, versionid
            )
        )
        return idf_file
    else:
        # execute transitions
        with TemporaryDirectory(
            prefix="transition_run_", suffix=simulname, dir=out_dir
        ) as tmp:
            # Move to temporary transition_run folder
            log(f"temporary dir ({Path(tmp).expand()}) created", lg.DEBUG)
            idf_file = Path(idf_file.copy(tmp)).abspath()  # copy and return abspath
            try:
                _execute_transitions(idf_file, to_version, versionid, **kwargs)
            except (CalledProcessError, EnergyPlusProcessError) as e:
                raise e

            # retrieve transitioned file
            for f in Path(tmp).files("*.idfnew"):
                if overwrite:
                    file = f.copy(out_dir / idf_file.basename())
                else:
                    file = f.copy(out_dir)
        return file


def _check_version(idf_file, to_version, out_dir):
    versionid = get_idf_version(idf_file, doted=False)[0:5]
    doted_version = get_idf_version(idf_file, doted=True)
    iddfile = getiddfile(doted_version)
    if os.path.exists(iddfile):
        # if a E+ exists, means there is an E+ install that can be used
        if versionid == to_version:
            # if version of idf file is equal to intended version, copy file from
            # temp transition folder into cache folder and return path
            return to_version, versionid
    # might be an old version of E+
    elif tuple(map(int, doted_version.split("."))) < (8, 0):
        # the version is an old E+ version (< 8.0)
        iddfile = getoldiddfile(doted_version)
        if versionid == to_version:
            # if version of idf file is equal to intended version, copy file from
            # temp transition folder into cache folder and return path
            return idf_file.copy(out_dir / idf_file.basename())
    # use to_version
    if to_version is None:
        # What is the latest E+ installed version
        to_version = latest_energyplus_version()
    if tuple(versionid.split("-")) > tuple(to_version.split("-")):
        raise EnergyPlusVersionError(idf_file, versionid, to_version)
    return to_version, versionid


@deprecated(
    deprecated_in="1.3.5",
    removed_in="1.4",
    current_version=archetypal.__version__,
    details="Use :func:`IDF._execute_transitions` instead",
)
def _execute_transitions(idf_file, to_version, versionid, **kwargs):
    """build a list of command line arguments"""
    vupdater_path = (
        get_eplus_dirs(settings.ep_version) / "PreProcess" / "IDFVersionUpdater"
    )
    exe = ".exe" if platform.system() == "Windows" else ""
    trans_exec = {
        "1-0-0": vupdater_path / "Transition-V1-0-0-to-V1-0-1" + exe,
        "1-0-1": vupdater_path / "Transition-V1-0-1-to-V1-0-2" + exe,
        "1-0-2": vupdater_path / "Transition-V1-0-2-to-V1-0-3" + exe,
        "1-0-3": vupdater_path / "Transition-V1-0-3-to-V1-1-0" + exe,
        "1-1-0": vupdater_path / "Transition-V1-1-0-to-V1-1-1" + exe,
        "1-1-1": vupdater_path / "Transition-V1-1-1-to-V1-2-0" + exe,
        "1-2-0": vupdater_path / "Transition-V1-2-0-to-V1-2-1" + exe,
        "1-2-1": vupdater_path / "Transition-V1-2-1-to-V1-2-2" + exe,
        "1-2-2": vupdater_path / "Transition-V1-2-2-to-V1-2-3" + exe,
        "1-2-3": vupdater_path / "Transition-V1-2-3-to-V1-3-0" + exe,
        "1-3-0": vupdater_path / "Transition-V1-3-0-to-V1-4-0" + exe,
        "1-4-0": vupdater_path / "Transition-V1-4-0-to-V2-0-0" + exe,
        "2-0-0": vupdater_path / "Transition-V2-0-0-to-V2-1-0" + exe,
        "2-1-0": vupdater_path / "Transition-V2-1-0-to-V2-2-0" + exe,
        "2-2-0": vupdater_path / "Transition-V2-2-0-to-V3-0-0" + exe,
        "3-0-0": vupdater_path / "Transition-V3-0-0-to-V3-1-0" + exe,
        "3-1-0": vupdater_path / "Transition-V3-1-0-to-V4-0-0" + exe,
        "4-0-0": vupdater_path / "Transition-V4-0-0-to-V5-0-0" + exe,
        "5-0-0": vupdater_path / "Transition-V5-0-0-to-V6-0-0" + exe,
        "6-0-0": vupdater_path / "Transition-V6-0-0-to-V7-0-0" + exe,
        "7-0-0": vupdater_path / "Transition-V7-0-0-to-V7-1-0" + exe,
        "7-1-0": vupdater_path / "Transition-V7-1-0-to-V7-2-0" + exe,
        "7-2-0": vupdater_path / "Transition-V7-2-0-to-V8-0-0" + exe,
        "8-0-0": vupdater_path / "Transition-V8-0-0-to-V8-1-0" + exe,
        "8-1-0": vupdater_path / "Transition-V8-1-0-to-V8-2-0" + exe,
        "8-2-0": vupdater_path / "Transition-V8-2-0-to-V8-3-0" + exe,
        "8-3-0": vupdater_path / "Transition-V8-3-0-to-V8-4-0" + exe,
        "8-4-0": vupdater_path / "Transition-V8-4-0-to-V8-5-0" + exe,
        "8-5-0": vupdater_path / "Transition-V8-5-0-to-V8-6-0" + exe,
        "8-6-0": vupdater_path / "Transition-V8-6-0-to-V8-7-0" + exe,
        "8-7-0": vupdater_path / "Transition-V8-7-0-to-V8-8-0" + exe,
        "8-8-0": vupdater_path / "Transition-V8-8-0-to-V8-9-0" + exe,
        "8-9-0": vupdater_path / "Transition-V8-9-0-to-V9-0-0" + exe,
        "9-0-0": vupdater_path / "Transition-V9-0-0-to-V9-1-0" + exe,
        "9-1-0": vupdater_path / "Transition-V9-1-0-to-V9-2-0" + exe,
    }

    transitions = [
        key
        for key in trans_exec
        if tuple(map(int, key.split("-"))) < tuple(map(int, to_version.split("-")))
        and tuple(map(int, key.split("-"))) >= tuple(map(int, versionid.split("-")))
    ]
    position = kwargs.get("position", None)
    for trans in tqdm(transitions, position=position, desc=f"file #{position}"):
        if not trans_exec[trans].exists():
            raise EnergyPlusProcessError(
                cmd=trans_exec[trans],
                stderr="The specified EnergyPlus version (v{}) does not have"
                " the required transition program '{}' in the "
                "PreProcess folder. See the documentation "
                "(archetypal.readthedocs.io/troubleshooting.html#missing"
                "-transition-programs) "
                "to solve this issue".format(to_version, trans_exec[trans]),
                idf=idf_file.basename(),
            )
        else:
            cmd = [trans_exec[trans], idf_file]
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=vupdater_path,
                )
                process_output, error_output = process.communicate()
                log(process_output.decode("utf-8"), lg.DEBUG)
            except CalledProcessError as exception:
                log(
                    "{} failed with error\n".format(
                        idf_version_updater.__name__, str(exception)
                    ),
                    lg.ERROR,
                )


def latest_energyplus_version():
    """Finds all installed versions of EnergyPlus in the default location and
    returns the latest version number.

    Returns:
        (EnergyPlusVersion): The version number of the latest E+ install
    """

    eplus_homes = get_eplus_basedirs()

    # check if any EnergyPlus install exists
    if not eplus_homes:
        raise Exception(
            "No EnergyPlus installation found. Make sure you have EnergyPlus "
            "installed. "
            "Go to https://energyplus.net/downloads to download the latest version of "
            "EnergyPlus."
        )

    # Find the most recent version of EnergyPlus installed from the version
    # number (at the end of the folder name)

    return sorted(
        (
            parse(re.search(r"([\d])-([\d])-([\d])", home.stem).group())
            for home in eplus_homes
        ),
        reverse=True,
    )[0]


def get_idf_version(file, doted=True):
    """Get idf version quickly by reading first few lines of idf file containing
    the 'VERSION' identifier

    Args:
        file (str or StringIO): Absolute or relative Path to the idf file
        doted (bool, optional): Wheter or not to return the version number

    Returns:
        str: the version id
    """
    if isinstance(file, StringIO):
        file.seek(0)
        txt = file.read()
    else:
        with open(os.path.abspath(file), "r", encoding="latin-1") as fhandle:
            txt = fhandle.read()
    try:
        ntxt = parse_idd.nocomment(txt, "!")
        blocks = ntxt.split(";")
        blocks = [block.strip() for block in blocks]
        bblocks = [block.split(",") for block in blocks]
        bblocks1 = [[item.strip() for item in block] for block in bblocks]
        ver_blocks = [block for block in bblocks1 if block[0].upper() == "VERSION"]
        ver_block = ver_blocks[0]
        if doted:
            versionid = ver_block[1]
        else:
            versionid = ver_block[1].replace(".", "-") + "-0"
    except Exception as e:
        log('Version id for file "{}" cannot be found'.format(file))
        log("{}".format(e))
        raise
    else:
        return versionid


def getoldiddfile(versionid):
    """find the IDD file of the E+ installation E+ version 7 and earlier have
    the idd in /EnergyPlus-7-2-0/bin/Energy+.idd

    Args:
        versionid:
    """
    vlist = versionid.split(".")
    if len(vlist) == 1:
        vlist = vlist + ["0", "0"]
    elif len(vlist) == 2:
        vlist = vlist + ["0"]
    ver_str = "-".join(vlist)
    eplus_exe, _ = eppy.runner.run_functions.install_paths(ver_str)
    eplusfolder = os.path.dirname(eplus_exe)
    iddfile = "{}/bin/Energy+.idd".format(eplusfolder)
    return iddfile


if __name__ == "__main__":
    pass
