# -*- coding: utf-8 -*-
# Copyright (c) 2018-2021, earthobservations developers.
# Distributed under the MIT License. See LICENSE for more info.
import logging
from abc import abstractmethod
from copy import copy
from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple, Union

import dateutil.parser
import numpy as np
import pandas as pd
import pytz
from measurement.measures import Distance
from measurement.utils import guess
from rapidfuzz import fuzz, process

from wetterdienst.core.core import Core
from wetterdienst.core.scalar.result import StationsResult
from wetterdienst.exceptions import InvalidEnumeration, StartDateEndDateError
from wetterdienst.metadata.columns import Columns
from wetterdienst.metadata.datarange import DataRange
from wetterdienst.metadata.kind import Kind
from wetterdienst.metadata.parameter import Parameter
from wetterdienst.metadata.period import Period, PeriodType
from wetterdienst.metadata.provider import Provider
from wetterdienst.metadata.resolution import Frequency, Resolution, ResolutionType
from wetterdienst.settings import Settings
from wetterdienst.util.enumeration import parse_enumeration_from_template
from wetterdienst.util.geo import Coordinates, derive_nearest_neighbours

log = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371


class ScalarRequestCore(Core):
    """Core for stations information of a source"""

    @property
    @abstractmethod
    def provider(self) -> Provider:
        """Optional enumeration for multiple resolutions"""
        pass

    @property
    @abstractmethod
    def kind(self) -> Kind:
        """Optional enumeration for multiple resolutions"""
        pass

    @property
    @abstractmethod
    def _resolution_base(self) -> Optional[Resolution]:
        """Optional enumeration for multiple resolutions"""
        pass

    @property
    @abstractmethod
    def _resolution_type(self) -> ResolutionType:
        """Resolution type, multi, fixed, ..."""
        pass

    @property
    def frequency(self) -> Frequency:
        """Frequency for the given resolution, used to create a full date range for
        mering"""
        return Frequency[self.resolution.name]

    @property
    @abstractmethod
    def _period_type(self) -> PeriodType:
        """Period type, fixed, multi, ..."""
        pass

    @property
    @abstractmethod
    def _period_base(self) -> Optional[Period]:
        """Period base enumeration from which a period string can be parsed"""
        pass

    @property
    def _parameter_base(self) -> Enum:
        """parameter base enumeration from which parameters can be parsed e.g.
        DWDObservationParameter"""
        if self._has_datasets:
            if not self._unique_dataset:
                raise NotImplementedError(
                    "implement _parameter_base enumeration that has "
                    "all parameters of one resolution stored in one place"
                )
        return

    @property
    @abstractmethod
    def _data_range(self) -> DataRange:
        """State whether data from this provider is given in fixed data chunks
        or has to be defined over start and end date"""
        pass

    @property
    @abstractmethod
    def _has_datasets(self) -> bool:
        """Boolean if weather service has datasets (when multiple parameters are stored
        in one table/file)"""
        pass

    @property
    def _dataset_base(self) -> Optional[Enum]:
        """Dataset base that is used to differ between different datasets"""
        if self._has_datasets:
            raise NotImplementedError("implement _dataset_base enumeration that contains available datasets")

    @property
    def _dataset_tree(self) -> Optional[object]:
        """Detailed dataset tree with all parameters per dataset"""
        if self._has_datasets:
            raise NotImplementedError(
                "implement _dataset_tree class that contains available datasets " "and their parameters"
            )

    @property
    def _unique_dataset(self) -> bool:
        """If ALL parameters are stored in one dataset e.g. all daily data is stored in
        one file"""
        if self._has_datasets:
            raise NotImplementedError("define if only one big dataset is available")
        return False

    @property
    def _dataset_accessor(self) -> str:
        """Accessor for dataset, by default the resolution is used as we expect
        datasets to be divided in resolutions but for some e.g. DWD Mosmix
        datasets are divided in another way (SMALL/LARGE in this case)"""
        return self.resolution.name

    @property
    @abstractmethod
    def _has_tidy_data(self) -> bool:
        """If data is generally provided tidy -> then data should not be tidied but
        rather tabulated if data is requested to not being tidy"""
        pass

    @property
    def _parameter_to_dataset_mapping(self) -> dict:
        """Mapping to go from a (flat) parameter to dataset"""
        if not self._unique_dataset:
            raise NotImplementedError("for non unique datasets implement a mapping from parameter to dataset")
        return {}

    @property
    @abstractmethod
    def _unit_tree(self):
        pass

    @property
    def datasets(self):
        datasets = self._dataset_tree[self._dataset_accessor].__dict__.keys()

        return list(filter(lambda x: x not in ("__module__", "__doc__"), datasets))

    @property
    @abstractmethod
    def _values(self):
        """Class to get the values for a request"""
        pass

    # Columns that should be contained within any stations information
    _base_columns = (
        Columns.STATION_ID.value,
        Columns.FROM_DATE.value,
        Columns.TO_DATE.value,
        Columns.HEIGHT.value,
        Columns.LATITUDE.value,
        Columns.LONGITUDE.value,
        Columns.NAME.value,
        Columns.STATE.value,
    )
    # TODO: eventually this can be matched with the type coercion of station data to get
    #  similar types of floats and strings
    # Dtype mapping for stations
    _dtype_mapping = {
        Columns.STATION_ID.value: str,
        Columns.HEIGHT.value: float,
        Columns.LATITUDE.value: float,
        Columns.LONGITUDE.value: float,
        Columns.NAME.value: str,
        Columns.STATE.value: str,
    }

    def _parse_period(self, period: Period) -> Optional[List[Period]]:
        """
        Method to parse period(s)

        :param period:
        :return:
        """
        if not period:
            return None
        elif self._period_type == PeriodType.FIXED:
            return [period]
        else:
            return (
                pd.Series(period)
                .apply(parse_enumeration_from_template, args=(self._period_base, Period))
                .sort_values()
                .tolist()
            )

    def _parse_parameter(self, parameter: List[Union[str, Enum]]) -> List[Tuple[Enum, Enum]]:
        """
        Method to parse parameters, either from string or enum. Case independent for
        strings.

        :param parameter: parameters as strings or enumerations
        :return: list of parameter enumerations of type self._parameter_base
        """
        # TODO: refactor this!
        # for logging
        enums = []
        if self._dataset_base:
            enums.append(self._dataset_base)

        enums.append(self._parameter_base)

        parameters = []

        for parameter in pd.Series(parameter):

            # Each parameter can either be
            #  - a dataset : gets all data from the dataset
            #  - a parameter : gets prefixed parameter from a resolution e.g.
            #      precipitation height of daily values is taken from climate summary
            #  - a tuple of parameter -> dataset : to decide from which dataset
            #    the parameter is taken
            try:
                parameter, dataset = pd.Series(parameter)
            except (ValueError, TypeError):
                parameter, dataset = parameter, parameter

            # Prefix return values
            parameter_, dataset_ = None, None

            # Try to parse dataset
            try:
                dataset_ = parse_enumeration_from_template(dataset, self._dataset_base)
            except InvalidEnumeration:
                pass

            if parameter == dataset and dataset_:
                parameters.append((dataset_, dataset_))
                continue

            try:
                # First parse parameter
                parameter_ = parse_enumeration_from_template(parameter, self._parameter_base[self._dataset_accessor])
            except (InvalidEnumeration, TypeError):
                pass
            else:
                if self._unique_dataset:
                    # If unique dataset the dataset is given by the accessor
                    # and the parameter is not a subset of a dataset
                    dataset_ = self._dataset_base[self._dataset_accessor]
                elif not dataset_:
                    # If there's multiple datasets the mapping defines which one
                    # is taken for the given parameter
                    dataset_ = self._parameter_to_dataset_mapping[self.resolution][parameter_]

                if not self._unique_dataset:
                    # Parameter then has to be taken from the datasets definition
                    parameter_ = self._dataset_tree[self._dataset_accessor][dataset_.name][parameter_.name]

                parameters.append((parameter_, dataset_))

            if not parameter_:
                log.info(f"parameter {parameter} could not be parsed from ({enums})")

        return parameters

    @staticmethod
    def _parse_station_id(series: pd.Series) -> pd.Series:
        """
        Dedicated method for parsing station ids, by default uses the same method as
        parse_strings but could be modified by the implementation class

        :param series:
        :return:
        """
        return series.astype(str)

    def __eq__(self, other) -> bool:
        """Equal method of request object"""
        return (
            self.parameter == other.parameter
            and self.resolution == other.resolution
            and self.period == other.period
            and self.start_date == other.start_date
            and self.end_date == other.end_date
            and self.humanize == other.humanize
            and self.tidy == other.tidy
        )

    def __init__(
        self,
        parameter: Tuple[Union[str, Enum]],
        resolution: Resolution,
        period: Period,
        start_date: Optional[Union[str, datetime, pd.Timestamp]] = None,
        end_date: Optional[Union[str, datetime, pd.Timestamp]] = None,
    ) -> None:
        """

        :param parameter: requested parameter(s)
        :param resolution: requested resolution
        :param period: requested period(s)
        :param start_date: Start date for filtering stations for their available data
        :param end_date:   End date for filtering stations for their available data
        :param humanize: boolean if parameters should be humanized
        :param tidy: boolean if data should be tidied
        :param si_units: boolean if values should be converted to si units
        """

        super().__init__()

        self.resolution = parse_enumeration_from_template(resolution, self._resolution_base, Resolution)
        self.period = self._parse_period(period)

        self.start_date, self.end_date = self.convert_timestamps(start_date, end_date)
        self.parameter = self._parse_parameter(parameter)

        self.humanize = copy(Settings.humanize)

        tidy = copy(Settings.tidy)
        if self._has_datasets:
            tidy = tidy or any([parameter not in self._dataset_base for parameter, dataset in self.parameter])
        self.tidy = tidy

        self.si_units = copy(Settings.si_units)

        log.info(
            f"Processing request for "
            f"provider={self.provider}, "
            f"parameter={self.parameter}, "
            f"resolution={self.resolution}, "
            f"period={self.period}, "
            f"start_date={self.start_date}, "
            f"end_date={self.end_date}, "
            f"humanize={self.humanize}, "
            f"tidy={self.tidy}, "
            f"si_units={self.si_units}"
        )

    @staticmethod
    def convert_timestamps(
        start_date: Optional[Union[str, datetime, pd.Timestamp]] = None,
        end_date: Optional[Union[str, datetime, pd.Timestamp]] = None,
    ) -> Union[Tuple[None, None], Tuple[pd.Timestamp, pd.Timestamp]]:
        """
        Sort out start_date vs. end_date, parse strings to datetime
        objects and finally convert both to pd.Timestamp types.

        :param start_date: Start date for filtering stations for their available data
        :param end_date:   End date for filtering stations for their available data
        :return:           pd.Timestamp objects tuple of (start_date, end_date)
        """

        if start_date is None and end_date is None:
            return None, None

        if start_date:
            if isinstance(start_date, str):
                start_date = dateutil.parser.isoparse(start_date)
            if not start_date.tzinfo:
                start_date = start_date.replace(tzinfo=pytz.UTC)

        if end_date:
            if isinstance(end_date, str):
                end_date = dateutil.parser.isoparse(end_date)
            if not end_date.tzinfo:
                end_date = end_date.replace(tzinfo=pytz.UTC)

        # If only one date given, set the other one to equal.
        if not start_date:
            start_date = end_date

        if not end_date:
            end_date = start_date

        # TODO: replace this with a response + logging
        if not start_date <= end_date:
            raise StartDateEndDateError("Error: 'start_date' must be smaller or equal to 'end_date'.")

        return pd.Timestamp(start_date), pd.Timestamp(end_date)

    @staticmethod
    def _format_unit(unit) -> str:
        """
        Method to format unit and create a string
        :param unit: pint Unit
        :return: unit as string
        """
        try:
            unit = unit.units
        except AttributeError:
            pass

        unit_string = format(unit, "~")

        if unit_string == "":
            return "-"

        return unit_string

    @classmethod
    def discover(cls, filter_=None, dataset=None, flatten: bool = True) -> dict:
        """
        Function to print/discover available parameters

        :param filter_:
        :param dataset:
        :param flatten:
        :return:
        """
        # TODO: Refactor this!!!
        flatten = cls._unique_dataset or flatten

        filter_ = cls._setup_discover_filter(filter_)

        if flatten:
            if dataset:
                log.warning("dataset filter will be ignored due to 'flatten'")

            parameters = {}

            for f in filter_:
                parameters[f.name.lower()] = {}
                for parameter in cls._parameter_base[f.name]:
                    parameters[f.name.lower()][parameter.name.lower()] = {}

                    if cls._unique_dataset:
                        origin_unit, si_unit = cls._unit_tree[f.name][parameter.name].value
                    else:
                        dataset = cls._parameter_to_dataset_mapping[f][parameter]

                        origin_unit, si_unit = cls._unit_tree[f.name][dataset.name][parameter.name].value

                    parameters[f.name.lower()][parameter.name.lower()]["origin"] = cls._format_unit(origin_unit)

                    parameters[f.name.lower()][parameter.name.lower()]["si"] = cls._format_unit(si_unit)

            return parameters

        datasets_filter = (
            pd.Series(dataset, dtype=str).apply(parse_enumeration_from_template, args=(cls._dataset_base,)).tolist()
            or cls._dataset_base
        )

        datasets_filter = [ds.name for ds in datasets_filter]

        parameters = {}

        for f in filter_:
            parameters[f.name.lower()] = {}

            for dataset in cls._dataset_tree[f.name].__dict__:
                if dataset.startswith("_") or dataset not in datasets_filter:
                    continue

                parameters[f.name.lower()][dataset.lower()] = {}

                for parameter in cls._dataset_tree[f.name][dataset]:

                    parameters[f.name.lower()][dataset.lower()][parameter.name.lower()] = {}

                    origin_unit, si_unit = cls._unit_tree[f.name][dataset][parameter.name].value

                    parameters[f.name.lower()][dataset.lower()][parameter.name.lower()]["origin"] = cls._format_unit(
                        origin_unit
                    )

                    parameters[f.name.lower()][dataset.lower()][parameter.name.lower()]["si"] = cls._format_unit(
                        si_unit
                    )

        return parameters

    @classmethod
    def _setup_discover_filter(cls, filter_) -> list:
        """
            Helper method to create filter for discover method, can be overwritten by
            subclasses to use other then the resolution for filtering

        :param filter_: typically resolution, if used in subclass can be directed
            towards something else
        :return:
        """
        if not filter_:
            filter_ = [*cls._resolution_base]

        return (
            pd.Series(filter_).apply(parse_enumeration_from_template, args=(cls._resolution_base, Resolution)).tolist()
        )

    def _coerce_meta_fields(self, df) -> pd.DataFrame:
        """
        Method for metadata column coercion.

        :param df: DataFrame with columns as strings
        :return: DataFrame with columns coerced to date etc.
        """
        df = df.astype(self._dtype_mapping)

        df[Columns.FROM_DATE.value] = pd.to_datetime(
            df[Columns.FROM_DATE.value], infer_datetime_format=True
        ).dt.tz_localize(pytz.UTC)
        df[Columns.TO_DATE.value] = pd.to_datetime(
            df[Columns.TO_DATE.value], infer_datetime_format=True
        ).dt.tz_localize(pytz.UTC)

        return df

    @abstractmethod
    def _all(self) -> pd.DataFrame:
        """
        Abstract method for gathering of sites information for a given implementation.
        Information consist of a DataFrame with station ids, location, name, etc

        :return: pandas.DataFrame with the information of different available sites
        """
        pass

    def all(self) -> StationsResult:
        """
        Wraps the _all method and applies date filters.

        :return: pandas.DataFrame with the information of different available stations
        """
        df = self._all()

        df = df.reindex(columns=self._base_columns)

        df = self._coerce_meta_fields(df)

        return StationsResult(self, df.copy().reset_index(drop=True))

    def filter_by_station_id(self, station_id: Tuple[str, ...]) -> StationsResult:
        """
        Method to filter stations by station ids

        :param station_id: list of stations that are requested
        :return: df with filtered stations
        """
        df = self.all().df

        station_id = self._parse_station_id(pd.Series(station_id))

        log.info(f"Filtering for station_id={list(station_id)}")

        df = df[df[Columns.STATION_ID.value].isin(station_id)]

        return StationsResult(self, df)

    def filter_by_name(self, name: str, first: bool = True, threshold: int = 90) -> StationsResult:
        """
        Method to filter stations for station name using string comparison.

        :param name: name of looked up station
        :param first: boolean if only first station is returned
        :param threshold: threshold for string match 0...100
        :return: df with matched station
        """
        if first:
            extract_fun = process.extractOne
        else:
            extract_fun = process.extract

        threshold = int(threshold)

        if threshold < 0:
            raise ValueError("threshold must be ge 0")

        df = self.all().df

        station_match = extract_fun(
            query=name,
            choices=df[Columns.NAME.value],
            scorer=fuzz.token_set_ratio,
            score_cutoff=threshold,
        )

        if station_match:
            if first:
                station_match = [station_match]
            station_name = pd.Series(station_match).apply(lambda x: x[0])

            df = df[df[Columns.NAME.value].isin(station_name)]

            df = df.reset_index(drop=True)
        else:
            df = pd.DataFrame().reindex(columns=df.columns)

        return StationsResult(stations=self, df=df)

    def filter_by_rank(
        self,
        latitude: float,
        longitude: float,
        rank: int,
    ) -> StationsResult:
        """
        Wrapper for get_nearby_stations_by_number using the given parameter set. Returns
        nearest stations defined by number.

        :param latitude: latitude in degrees
        :param longitude: longitude in degrees
        :param rank: number of stations to be returned, greater 0
        :return: pandas.DataFrame with station information for the selected stations
        """
        rank = int(rank)

        if rank <= 0:
            raise ValueError("'num_stations_nearby' has to be at least 1.")

        coords = Coordinates(np.array(latitude), np.array(longitude))

        df = self.all().df.reset_index(drop=True)

        distances, indices_nearest_neighbours = derive_nearest_neighbours(
            df[Columns.LATITUDE.value].values,
            df[Columns.LONGITUDE.value].values,
            coords,
            min(rank, df.shape[0]),
        )

        df = df.iloc[indices_nearest_neighbours.flatten(), :].reset_index(drop=True)

        df[Columns.DISTANCE.value] = distances.flatten() * EARTH_RADIUS_KM

        if df.empty:
            log.warning(
                f"No weather stations were found for coordinate " f"{latitude}°N and {longitude}°E and number {rank}"
            )

        return StationsResult(self, df.reset_index(drop=True))

    def filter_by_distance(
        self, latitude: float, longitude: float, distance: float, unit: str = "km"
    ) -> StationsResult:
        """
        Wrapper for get_nearby_stations_by_distance using the given parameter set.
        Returns nearest stations defined by distance (km).

        :param latitude: latitude in degrees
        :param longitude: longitude in degrees
        :param distance: distance (km) for which stations will be selected
        :param unit: unit string for conversion
        :return: pandas.DataFrame with station information for the selected stations
        """
        distance = float(distance)

        # Theoretically a distance of 0 km is possible
        if distance < 0:
            raise ValueError("'distance' has to be at least 0.0")

        unit = unit.strip()

        distance_in_km = guess(distance, unit, [Distance]).km

        # TODO: replace the repeating call to self.all()
        all_nearby_stations = self.filter_by_rank(latitude, longitude, self.all().df.shape[0]).df

        df = all_nearby_stations[all_nearby_stations[Columns.DISTANCE.value] <= distance_in_km]

        if df.empty:
            log.warning(
                f"No weather stations were found for coordinate "
                f"{latitude}°N and {longitude}°E and distance {distance_in_km}km"
            )

        return StationsResult(stations=self, df=df.reset_index(drop=True))

    def filter_by_bbox(self, left: float, bottom: float, right: float, top: float) -> StationsResult:
        """
        Method to filter stations by bounding box.

        :param bottom: bottom latitude as float
        :param left: left longitude as float
        :param top: top latitude as float
        :param right: right longitude as float
        :return: df with stations in bounding box
        """
        left, bottom, right, top = float(left), float(bottom), float(right), float(top)

        if left >= right:
            raise ValueError("bbox left border should be smaller then right")

        if bottom >= top:
            raise ValueError("bbox bottom border should be smaller then top")

        lat_interval = pd.Interval(bottom, top, closed="both")
        lon_interval = pd.Interval(left, right, closed="both")

        df = self.all().df

        df = df[
            df[Columns.LATITUDE.value].apply(lambda x: x in lat_interval)
            & df[Columns.LONGITUDE.value].apply(lambda x: x in lon_interval)
        ]

        return StationsResult(stations=self, df=df.reset_index(drop=True))

    def filter_by_sql(self, sql: str) -> pd.DataFrame:
        """

        :param sql:
        :return:
        """
        import duckdb

        df = self.all().df

        df = duckdb.query_df(df, "data", sql).df()

        df[Columns.FROM_DATE.value] = df[Columns.FROM_DATE.value].dt.tz_localize(self.tz)
        df[Columns.TO_DATE.value] = df[Columns.TO_DATE.value].dt.tz_localize(self.tz)

        return StationsResult(stations=self, df=df.reset_index(drop=True))

    def interpolate(self, latitude, longitude) -> None:
        """
        Method to interpolate values

        :param latitude:
        :param longitude:
        :return:
        """

        # TODO: should we disallow interpolation for data "intense" resolutions?
        if self.resolution in (
            Resolution.MINUTE_1,
            Resolution.MINUTE_5,
            Resolution.MINUTE_5,
            Resolution.MINUTE_10,
        ):
            raise NotImplementedError(
                "interpolation is not currently allowed for high resolutions due to mass of data"
            )

        # This should be defined somewhere else and we may differ between
        #   - heterogeneous parameters such as precipitation_height
        #   - homogeneous parameters such as temperature_air_200
        interpolatable_parameters = [Parameter.TEMPERATURE_AIR_MEAN_200.name]

        for parameter, dataset in self.parameter:
            if parameter == dataset:
                log.info("only individual parameters can be interpolated")
                continue

            if parameter not in interpolatable_parameters:
                log.info(f"parameter {parameter.name} can not be interpolated")
                continue

            stations, values = self._get_interpolation_stations_and_values(
                latitude=latitude, longitude=longitude
            )

            # Run through values and interpolate
            interpolated_values = pd.DataFrame()
            for i in values.iterrows():
                # first three available stations
                available_stations = i.dropna().columns

                locations = stations.loc[stations[Columns.STATION_ID.value].isin(available_stations)]

                # Calculate value here

            return InterpolatedValuesResult(interpolated_values)

    def _get_interpolation_stations_and_values(
        self, latitude: float, longitude: float
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        critical_threshold = 0.05

        # TODO: rank should be stepwise increased until we have enough stations for interpolation
        # TODO: we will first have to find our first k stations that are minimum requirement for
        #  interpolation
        # TODO: we will have to make sure that the set of those k stations has at least one day
        #   of data in common, obviously this is only our minimum requirement
        # TODO: we will have to select a representative station that should be used to calculate
        #  correlations with other stations. This will help us define a stoppage criteria so that
        #   no further stations are being included. Say we have set a minimum correlation of 0.8 .
        #   then with the help of the representative station we can stop including more and more
        #   stations by calculating the correlation and aborting when getting below 0.8 .
        stations_ranked = self.filter_by_rank(
            latitude=latitude, longitude=longitude, rank=20
        )

        interpolation_stations = pd.DataFrame()

        # Store values of included stations
        # TODO: should we store data tidy or tabular? we will have to calculate the correlation
        #  probably more often
        values = pd.DataFrame()

        while interpolation_stations.shape[0] < 3:
            # TODO: Iterate through stations
            # Get first station
            stations_ranked
            # get values
            # check values, any not NaN?, any common values with other stations?
            # what if one station only has few values?
            if self._increase_of_value_sets(values) < critical_threshold:
                break

        return interpolation_stations, values

    @staticmethod
    def _increase_of_value_sets(values: pd.DataFrame, _store: dict = {}) -> float:
        """
        Method to calculate the increase of value sets within the last added station
        e.g. dates where a minimum of three values are available for interpolation
        :param values:
        :param _store: value store
        :return:
        """

        def _get_number_of_sets(df: pd.DataFrame) -> float:
            """Get number of value sets with at least 3 values"""
            return df.isna().apply(lambda x: np.nansum(x) > 2).sum()

        n = values.shape[1]  # use number of cols to store previous values result

        previous = _store.get(n)
        if not previous:
            previous = _get_number_of_sets(values.iloc[:, 1:-1])

        current = _get_number_of_sets(values.iloc[:, 1:])

        _store[n + 1] = current

        return (current / previous) - 1
