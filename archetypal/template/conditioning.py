################################################################################
# Module: archetypal.template
# Description:
# License: MIT, see full license in LICENSE.txt
# Web: https://github.com/samuelduchesne/archetypal
################################################################################

import collections
import logging as lg
import math
import sqlite3
from enum import Enum

import numpy as np
from deprecation import deprecated
from sigfig import round

import archetypal
from archetypal import ReportData, float_round, log, settings, timeit
from archetypal.template import UmiBase, UmiSchedule, UniqueName


class UmiBaseEnum(Enum):
    def __lt__(self, other):
        return self._value_ < other._value_

    def __gt__(self, other):
        return self._value_ > other._value_


class FuelType(Enum):
    """Fuel types taken from EnergyPlus 9.2 .idd file for OtherEquipment."""

    NONE = 0
    Electricity = 1
    NaturalGas = 2
    PropaneGas = 3
    FuelOil1 = 4
    FuelOil2 = 5
    Diesel = 6
    Gasoline = 7
    Coal = 8
    OtherFuel1 = 9
    OtherFuel2 = 10
    Steam = 11
    DistrictHeating = 12
    DistrictCooling = 13


class HeatRecoveryTypes(UmiBaseEnum):
    NONE = 0
    Enthalpy = 1
    Sensible = 2


class EconomizerTypes(UmiBaseEnum):
    NoEconomizer = 0
    DifferentialDryBulb = 1
    DifferentialEnthalphy = 2


class IdealSystemLimit(UmiBaseEnum):
    """LimitFlowRate means that the heating supply air flow rate will be
    limited to the value specified in the next input field. LimitCapacity means that
    the sensible heating capacity will be limited to the value specified in the
    Maximum Sensible Heating Capacity field. LimitFlowRateAndCapacity means that both
    flow rate and capacity will be limited. NoLimit (the default) means that there will
    not be any limit on the heating supply air flow rate or capacity and the subsequent
    two fields will be ignored."""

    NoLimit = 0
    LimitFlowRate = 1
    LimitCapacity = 2
    LimitFlowRateAndCapacity = 3


class ZoneConditioning(UmiBase):
    """HVAC settings for the zone

    .. image:: ../images/template/zoninfo-conditioning.png
    """

    def __init__(
        self,
        Name,
        IsHeatingOn=False,
        HeatingSetpoint=20,
        HeatingSchedule=None,
        HeatingLimitType=IdealSystemLimit.NoLimit,
        HeatingFuelType=FuelType.NaturalGas,
        MaxHeatingCapacity=100,
        MaxHeatFlow=100,
        HeatingCoeffOfPerf=1,
        IsCoolingOn=False,
        CoolingSetpoint=26,
        CoolingSchedule=None,
        CoolingLimitType=IdealSystemLimit.NoLimit,
        CoolingFuelType=FuelType.Electricity,
        MaxCoolingCapacity=100,
        MaxCoolFlow=100,
        CoolingCoeffOfPerf=1,
        IsMechVentOn=False,
        EconomizerType=EconomizerTypes.NoEconomizer,
        MechVentSchedule=None,
        MinFreshAirPerArea=0,
        MinFreshAirPerPerson=0,
        HeatRecoveryType=HeatRecoveryTypes.NONE,
        HeatRecoveryEfficiencyLatent=0.65,
        HeatRecoveryEfficiencySensible=0.7,
        **kwargs,
    ):
        """Initialize a new :class:`ZoneConditioning` object.

        Args:
            Name (str): Name of the object. Must be Unique.
            IsHeatingOn (bool): Whether or not heating is available.
            HeatingSetpoint (float): The temperature below which zone heating is
                turned on. Here, we take the mean value over the ye
            HeatingSchedule (UmiSchedule): The availability schedule for space
                heating in this zone. If the value is 0, heating is not
                available, and heating is not supplied to the zone.
            HeatingLimitType (int): The input must be either LimitFlowRate = 1,
                LimitCapacity = 2, LimitFlowRateAndCapacity = 3 or NoLimit = 0.
            MaxHeatingCapacity (float): The maximum allowed sensible heating
                capacity in Watts if Heating Limit is set to LimitCapacity or
                LimitFlowRateAndCapacity
            MaxHeatFlow (float): The maximum heating supply air flow rate in
                cubic meters per second if heating limit is set to LimitFlowRate
                or LimitFlowRateAndCapacity
            HeatingCoeffOfPerf (float): Efficiency of heating system. The COP is
                of each zone is equal, and refer to the COP of the entire
                building.
            IsCoolingOn (bool): Whether or not cooling is available.
            CoolingSetpoint (float): The temperature above which the zone
                heating is turned on. Here, we take the mean value over the
                ye
            CoolingSchedule (UmiSchedule): The availability schedule for space
                cooling in this zone. If the value is 0, cooling is not
                available, and cooling is not supplied to the zone.
            CoolingLimitType (str): The input must be either LimitFlowRate = 1,
                LimitCapacity = 2, LimitFlowRateAndCapacity = 3 or NoLimit = 0.
            MaxCoolingCapacity (float): The maximum allowed total (sensible plus
                latent) cooling capacity in Watts per square meter.
            MaxCoolFlow (float): The maximum cooling supply air flow rate in
                cubic meters per second if Cooling Limit is set to LimitFlowRate
                or LimitFlowRateAndCapacity
            CoolingCoeffOfPerf (float): Performance factor of the cooling
                system. This value is used to calculate the total cooling energy
                use by dividing the cooling load by the COP. The COP of the zone
                shared with all zones and refers to the COP of the entire
                building.
            IsMechVentOn (bool): If True, an outdoor air quantity for use by the
                model is calculated.
            EconomizerType (int): Specifies if there is an outdoor air
                economizer. The choices are: NoEconomizer = 0, DifferentialDryBulb = 1,
                or DifferentialEnthalpy = 2. For the moment, the EconomizerType is
                applied for the entire building (every zone with the same
                EconomizerType). Moreover, since UMI does not support all
                Economizer Types, some assumptions are made:

                - If 'NoEconomizer' in EnergyPlus, EconomizerType='NoEconomizer'
                - If 'DifferentialEnthalpy' in EnergyPlus,EconomizerType =
                  'DifferentialEnthalpy'
                - If 'DifferentialDryBulb' in EnergyPlus, EconomizerType =
                  'DifferentialDryBulb'
                - If 'FixedDryBulb' in EnergyPlus, EconomizerType =
                  'DifferentialDryBulb'
                - If 'FixedEnthalpy' in EnergyPlus, EconomizerType =
                  'DifferentialEnthalpy'
                - If 'ElectronicEnthalpy' in EnergyPlus, EconomizerType =
                  'DifferentialEnthalpy'
                - If 'FixedDewPointAndDryBulb' in EnergyPlus, EconomizerType =
                  'DifferentialDryBulb'
                - If 'DifferentialDryBulbAndEnthalpy' in EnergyPlus,
                  EconomizerType = 'DifferentialEnthalpy'
            MechVentSchedule (UmiSchedule): The availability schedule of the
                mechanical ventilation. If the value is 0, the mechanical
                ventilation is not available and air flow is not requested.
            MinFreshAirPerArea (flaot): The design outdoor air volume flow rate
                per square meter of floor area (units are m3/s-m2). This input
                is used if Outdoor Air Method is Flow/Area, Sum or Maximum
            MinFreshAirPerPerson (float): The design outdoor air volume flow
                rate per person for this zone in cubic meters per second per
                person. The default is 0.00944 (20 cfm per person).
            HeatRecoveryType (int): Select from None = 0, Sensible = 1, or
                Enthalpy = 2. None means that there is no heat recovery. Sensible
                means that there is sensible heat recovery whenever the zone
                exhaust air temperature is more favorable than the outdoor air
                temperature. Enthalpy means that there is latent and sensible
                heat recovery whenever the zone exhaust air enthalpy is more
                favorable than the outdoor air enthalpy. The default is None
            HeatRecoveryEfficiencyLatent (float): The latent heat recovery
                effectiveness, where effectiveness is defined as the change in
                supply humidity ratio divided by the difference in entering
                supply and relief air humidity ratios. The default is 0.65.

                - If the HeatExchanger is an AirToAir FlatPlate,
                  HeatRecoveryEfficiencyLatent = HeatRecoveryEfficiencySensible
                  - 0.05
                - If the HeatExchanger is an AirToAir SensibleAndLatent, we
                  suppose that HeatRecoveryEfficiencyLatent = Latent
                  Effectiveness at 100% Heating Air Flow
                - If the HeatExchanger is a Desiccant BalancedFlow, we use the
                  default value for the efficiency (=0.65).
            HeatRecoveryEfficiencySensible (float): The sensible heat recovery
                effectiveness, where effectiveness is defined as the change in
                supply temperature divided by the difference in entering supply
                and relief air temperatures. The default is 0.70.

                - If the HeatExchanger is an AirToAir FlatPlate,
                  HeatRecoveryEfficiencySensible = (Supply Air Outlet TÂ°C -
                  Supply Air Inlet TÂ°C)/(Secondary Air Inlet TÂ°C - Supply Air
                  Inlet TÂ°C)
                - If the HeatExchanger is an AirToAir SensibleAndLatent, we
                  suppose that HeatRecoveryEfficiencySensible = Sensible
                  Effectiveness at 100% Heating Air Flow
                - If the HeatExchanger is a Desiccant BalancedFlow, we use the
                  default value for the efficiency (=0.70)
            **kwargs: Other arguments passed to the base class
                :class:`archetypal.template.UmiBase`
        """
        super(ZoneConditioning, self).__init__(Name, **kwargs)
        self.MechVentSchedule = MechVentSchedule
        self.HeatingSchedule = HeatingSchedule
        self.CoolingSchedule = CoolingSchedule
        self.CoolingCoeffOfPerf = CoolingCoeffOfPerf
        self.CoolingLimitType = IdealSystemLimit(CoolingLimitType)
        self.CoolingFuelType = FuelType(CoolingFuelType)
        self.CoolingSetpoint = CoolingSetpoint
        self.EconomizerType = EconomizerTypes(EconomizerType)
        self.HeatRecoveryEfficiencyLatent = HeatRecoveryEfficiencyLatent
        self.HeatRecoveryEfficiencySensible = HeatRecoveryEfficiencySensible
        self.HeatRecoveryType = HeatRecoveryTypes(HeatRecoveryType)
        self.HeatingCoeffOfPerf = HeatingCoeffOfPerf
        self.HeatingLimitType = IdealSystemLimit(HeatingLimitType)
        self.HeatingFuelType = FuelType(HeatingFuelType)
        self.HeatingSetpoint = HeatingSetpoint
        self.IsCoolingOn = IsCoolingOn
        self.IsHeatingOn = IsHeatingOn
        self.IsMechVentOn = IsMechVentOn
        self.MaxCoolFlow = MaxCoolFlow
        self.MaxCoolingCapacity = MaxCoolingCapacity
        self.MaxHeatFlow = MaxHeatFlow
        self.MaxHeatingCapacity = MaxHeatingCapacity
        self.MinFreshAirPerArea = MinFreshAirPerArea
        self.MinFreshAirPerPerson = MinFreshAirPerPerson

        self._belongs_to_zone = kwargs.get("zone", None)

    @property
    def CoolingSetpoint(self):
        return float(self._cooling_setpoint)

    @CoolingSetpoint.setter
    def CoolingSetpoint(self, value):
        self._cooling_setpoint = value

    @property
    def HeatingSetpoint(self):
        return float(self._heating_setpoint)

    @HeatingSetpoint.setter
    def HeatingSetpoint(self, value):
        self._heating_setpoint = value

    @property
    def MaxCoolFlow(self):
        return float(self._MaxCoolFlow)

    @MaxCoolFlow.setter
    def MaxCoolFlow(self, value):
        self._MaxCoolFlow = value

    @property
    def MaxHeatFlow(self):
        return float(self._MaxHeatFlow)

    @MaxHeatFlow.setter
    def MaxHeatFlow(self, value):
        self._MaxHeatFlow = value

    @property
    def MaxHeatingCapacity(self):
        return float(self._MaxHeatingCapacity)

    @MaxHeatingCapacity.setter
    def MaxHeatingCapacity(self, value):
        self._MaxHeatingCapacity = value

    @property
    def MaxCoolingCapacity(self):
        return float(self._MaxCoolingCapacity)

    @MaxCoolingCapacity.setter
    def MaxCoolingCapacity(self, value):
        self._MaxCoolingCapacity = value

    @property
    def MinFreshAirPerArea(self):
        return float(self._min_fresh_air_per_area)

    @MinFreshAirPerArea.setter
    def MinFreshAirPerArea(self, value):
        self._min_fresh_air_per_area = value

    @property
    def MinFreshAirPerPerson(self):
        return float(self._min_fresh_air_per_person)

    @MinFreshAirPerPerson.setter
    def MinFreshAirPerPerson(self, value):
        self._min_fresh_air_per_person = value

    def __add__(self, other):
        return self.combine(other)

    def __hash__(self):
        return hash(
            (self.__class__.__name__, getattr(self, "Name", None), self.DataSource)
        )

    def __eq__(self, other):
        if not isinstance(other, ZoneConditioning):
            return False
        else:
            return all(
                [
                    self.CoolingCoeffOfPerf == other.CoolingCoeffOfPerf,
                    self.CoolingLimitType == other.CoolingLimitType,
                    self.CoolingSetpoint == other.CoolingSetpoint,
                    self.CoolingSchedule == other.CoolingSchedule,
                    self.EconomizerType == other.EconomizerType,
                    self.HeatRecoveryEfficiencyLatent
                    == other.HeatRecoveryEfficiencyLatent,
                    self.HeatRecoveryEfficiencySensible
                    == other.HeatRecoveryEfficiencySensible,
                    self.HeatRecoveryType == other.HeatRecoveryType,
                    self.HeatingCoeffOfPerf == other.HeatingCoeffOfPerf,
                    self.HeatingLimitType == other.HeatingLimitType,
                    self.HeatingSetpoint == other.HeatingSetpoint,
                    self.HeatingSchedule == other.HeatingSchedule,
                    self.IsCoolingOn == other.IsCoolingOn,
                    self.IsHeatingOn == other.IsHeatingOn,
                    self.IsMechVentOn == other.IsMechVentOn,
                    self.MaxCoolFlow == other.MaxCoolFlow,
                    self.MaxCoolingCapacity == other.MaxCoolingCapacity,
                    self.MaxHeatFlow == other.MaxHeatFlow,
                    self.MaxHeatingCapacity == other.MaxHeatingCapacity,
                    self.MinFreshAirPerArea == other.MinFreshAirPerArea,
                    self.MinFreshAirPerPerson == other.MinFreshAirPerPerson,
                    self.MechVentSchedule == other.MechVentSchedule,
                ]
            )

    @classmethod
    @deprecated(
        deprecated_in="1.3.1",
        removed_in="1.5",
        current_version=archetypal.__version__,
        details="Use from_dict function instead",
    )
    def from_json(cls, *args, **kwargs):

        return cls.from_dict(*args, **kwargs)

    @classmethod
    def from_dict(cls, *args, **kwargs):
        """
        Args:
            *args:
            **kwargs:
        """
        zc = cls(*args, **kwargs)

        cool_schd = kwargs.get("CoolingSchedule", None)
        zc.CoolingSchedule = zc.get_ref(cool_schd)
        heat_schd = kwargs.get("HeatingSchedule", None)
        zc.HeatingSchedule = zc.get_ref(heat_schd)
        mech_schd = kwargs.get("MechVentSchedule", None)
        zc.MechVentSchedule = zc.get_ref(mech_schd)
        return zc

    def to_json(self):
        """Convert class properties to dict"""
        self.validate()  # Validate object before trying to get json format

        data_dict = collections.OrderedDict()

        data_dict["$id"] = str(self.id)
        data_dict["CoolingSchedule"] = self.CoolingSchedule.to_dict()
        data_dict["CoolingCoeffOfPerf"] = round(self.CoolingCoeffOfPerf, 3)
        data_dict["CoolingSetpoint"] = (
            round(self.CoolingSetpoint, 3)
            if not math.isnan(self.CoolingSetpoint)
            else 26
        )
        data_dict["CoolingLimitType"] = self.CoolingLimitType.value
        data_dict["CoolingFuelType"] = self.CoolingFuelType.value
        data_dict["EconomizerType"] = self.EconomizerType.value
        data_dict["HeatingCoeffOfPerf"] = round(self.HeatingCoeffOfPerf, 3)
        data_dict["HeatingLimitType"] = self.HeatingLimitType.value
        data_dict["HeatingFuelType"] = self.HeatingFuelType.value
        data_dict["HeatingSchedule"] = self.HeatingSchedule.to_dict()
        data_dict["HeatingSetpoint"] = (
            round(self.HeatingSetpoint, 3)
            if not math.isnan(self.HeatingSetpoint)
            else 20
        )
        data_dict["HeatRecoveryEfficiencyLatent"] = self.HeatRecoveryEfficiencyLatent
        data_dict[
            "HeatRecoveryEfficiencySensible"
        ] = self.HeatRecoveryEfficiencySensible
        data_dict["HeatRecoveryType"] = self.HeatRecoveryType.value
        data_dict["IsCoolingOn"] = self.IsCoolingOn
        data_dict["IsHeatingOn"] = self.IsHeatingOn
        data_dict["IsMechVentOn"] = self.IsMechVentOn
        data_dict["MaxCoolFlow"] = round(self.MaxCoolFlow, 3)
        data_dict["MaxCoolingCapacity"] = round(self.MaxCoolingCapacity, 3)
        data_dict["MaxHeatFlow"] = round(self.MaxHeatFlow, 3)
        data_dict["MaxHeatingCapacity"] = round(self.MaxHeatingCapacity, 3)
        data_dict["MechVentSchedule"] = self.MechVentSchedule.to_dict()
        data_dict["MinFreshAirPerArea"] = round(self.MinFreshAirPerArea, 3)
        data_dict["MinFreshAirPerPerson"] = round(self.MinFreshAirPerPerson, 3)
        data_dict["Category"] = self.Category
        data_dict["Comments"] = self.Comments
        data_dict["DataSource"] = self.DataSource
        data_dict["Name"] = UniqueName(self.Name)

        return data_dict

    @classmethod
    @timeit
    def from_zone(cls, zone, nolimit=False, **kwargs):
        """
        Args:
            zone (archetypal.template.zone.Zone): zone to gets information from
        """
        # If Zone is not part of Conditioned Area, it should not have a ZoneLoad object.
        if zone.is_part_of_conditioned_floor_area and zone.is_part_of_total_floor_area:
            # First create placeholder object.
            name = zone.Name + "_ZoneConditioning"
            z_cond = cls(
                Name=name, zone=zone, idf=zone.idf, Category=zone.idf.name, **kwargs
            )
            z_cond._set_thermostat_setpoints(zone)
            z_cond._set_zone_cops(zone, nolimit=nolimit)
            z_cond._set_heat_recovery(zone)
            z_cond._set_mechanical_ventilation(zone)
            z_cond._set_economizer(zone)

            return z_cond
        else:
            return None

    def _set_economizer(self, zone):
        """Set economizer parameters

        Todo:
            - Here EconomizerType is for the entire building, try to do it for
              each zone.
            - Fix typo in DifferentialEnthalpy (extra h) when issue is resolved
              at Basilisk project:
              https://github.com/MITSustainableDesignLab/basilisk/issues/32

        Args:
            zone (Zone): The zone object.
        """
        # Economizer
        controllers_in_idf = zone.idf.idfobjects["Controller:OutdoorAir".upper()]
        self.EconomizerType = EconomizerTypes.NoEconomizer  # default value

        for object in controllers_in_idf:
            if object.Economizer_Control_Type == "NoEconomizer":
                self.EconomizerType = EconomizerTypes.NoEconomizer
            elif object.Economizer_Control_Type == "DifferentialEnthalphy":
                self.EconomizerType = EconomizerTypes.DifferentialEnthalphy
            elif object.Economizer_Control_Type == "DifferentialDryBulb":
                self.EconomizerType = EconomizerTypes.DifferentialDryBulb
            elif object.Economizer_Control_Type == "FixedDryBulb":
                self.EconomizerType = EconomizerTypes.DifferentialDryBulb
            elif object.Economizer_Control_Type == "FixedEnthalpy":
                self.EconomizerType = EconomizerTypes.DifferentialEnthalphy
            elif object.Economizer_Control_Type == "ElectronicEnthalpy":
                self.EconomizerType = EconomizerTypes.DifferentialEnthalphy
            elif object.Economizer_Control_Type == "FixedDewPointAndDryBulb":
                self.EconomizerType = EconomizerTypes.DifferentialDryBulb
            elif object.Economizer_Control_Type == "DifferentialDryBulbAndEnthalpy":
                self.EconomizerType = EconomizerTypes.DifferentialEnthalphy

    def _set_mechanical_ventilation(self, zone):
        """Mechanical Ventilation in UMI (or Archsim-based models) is applied to an
        `ZoneHVAC:IdealLoadsAirSystem` through the `Design Specification Outdoor Air
        Object Name` which in turn is a `DesignSpecification:OutdoorAir` object. It
        is this last object that performs the calculation for the outdoor air
        flowrate. Moreover, UMI defaults to the "sum" method, meaning that the
        Outdoor Air Flow per Person {m3/s-person} and the Outdoor Air Flow per Area {
        m3/s-m2} are summed to obtain the zone outdoor air flow rate. Moreover,
        not all models have the `DesignSpecification:OutdoorAir` object which poses a
        difficulty when trying to resolve the mechanical ventilation parameters.

        Two general cases exist: 1) models with a `Zone:Sizing` object (and possibly
        no `DesignSpecification:OutdoorAir`) and 2) models with

        Args:
            zone (Zone): The zone object.
        """
        # For models with ZoneSizes
        try:
            try:
                (
                    self.IsMechVentOn,
                    self.MinFreshAirPerArea,
                    self.MinFreshAirPerPerson,
                    self.MechVentSchedule,
                ) = self.fresh_air_from_zone_sizes(zone)
            except ValueError:
                (
                    self.IsMechVentOn,
                    self.MinFreshAirPerArea,
                    self.MinFreshAirPerPerson,
                    self.MechVentSchedule,
                ) = self.fresh_air_from_ideal_loads(zone)
        except:
            # Set elements to None so that .combine works correctly
            self.IsMechVentOn = False
            self.MinFreshAirPerPerson = 0
            self.MinFreshAirPerArea = 0
            self.MechVentSchedule = None

    @staticmethod
    def get_equipment_list(zone):
        # get zone equipment list
        connections = zone._epbunch.getreferingobjs(
            iddgroups=["Zone HVAC Equipment Connections"], fields=["Zone_Name"]
        )
        referenced_object = next(iter(connections)).get_referenced_object(
            "Zone_Conditioning_Equipment_List_Name"
        )
        # EquipmentList can have 18 objects. Filter out the None objects.
        return filter(
            None,
            [
                referenced_object.get_referenced_object(f"Zone_Equipment_{i}_Name")
                for i in range(1, 19)
            ],
        )

    def fresh_air_from_ideal_loads(self, zone):
        """

        Args:
            zone:

        Returns:
            4-tuple: (IsMechVentOn, MinFreshAirPerArea, MinFreshAirPerPerson, MechVentSchedule)
        """
        equip_list = self.get_equipment_list(zone)
        equipment = next(
            iter(
                [
                    eq
                    for eq in equip_list
                    if eq.key.lower() == "ZoneHVAC:IdealLoadsAirSystem".lower()
                ]
            )
        )
        oa_spec = equipment.get_referenced_object(
            "Design_Specification_Outdoor_Air_Object_Name"
        )
        oa_area = float(oa_spec.Outdoor_Air_Flow_per_Zone_Floor_Area)
        oa_person = float(oa_spec.Outdoor_Air_Flow_per_Person)
        mechvent_schedule = self._mechanical_schedule_from_outdoorair_object(
            oa_spec, zone
        )
        return True, oa_area, oa_person, mechvent_schedule

    def fresh_air_from_zone_sizes(self, zone):
        """Returns the Mechanical Ventilation from the ZoneSizes Table in the sql db.

        Args:
            zone:

        Returns:
            4-tuple: (IsMechVentOn, MinFreshAirPerArea, MinFreshAirPerPerson, MechVentSchedule)
        """
        import sqlite3

        import pandas as pd

        # create database connection with sqlite3
        with sqlite3.connect(zone.idf.sql_file) as conn:
            sql_query = f"""
                        select t.ColumnName, t.Value
                        from TabularDataWithStrings t
                        where TableName == 'Minimum Outdoor Air During Occupied Hours' and RowName == '{zone.Name.upper()}'"""
            oa = (
                pd.read_sql_query(sql_query, con=conn, coerce_float=True)
                .set_index("ColumnName")
                .squeeze()
            )
            oa = pd.to_numeric(oa)
            oa_design = oa["Zone Volume"] * oa["Mechanical Ventilation"] / 3600  # m3/s
            isoa = oa["Mechanical Ventilation"] > 0  # True if ach > 0
            oa_area = oa_design / zone.area
            oa_person = oa_design / oa["Nominal Number of Occupants"]

            designobjs = zone._epbunch.getreferingobjs(
                iddgroups=["HVAC Design Objects"], fields=["Zone_or_ZoneList_Name"]
            )
            obj = next(iter(eq for eq in designobjs if eq.key.lower() == "sizing:zone"))
            oa_spec = obj.get_referenced_object(
                "Design_Specification_Outdoor_Air_Object_Name"
            )
            mechvent_schedule = self._mechanical_schedule_from_outdoorair_object(
                oa_spec, zone
            )
            return isoa, oa_area, oa_person, mechvent_schedule

    def _mechanical_schedule_from_outdoorair_object(self, oa_spec, zone):
        if oa_spec.Outdoor_Air_Schedule_Name != "":
            umi_schedule = UmiSchedule(
                Name=oa_spec.Outdoor_Air_Schedule_Name, idf=zone.idf
            )
            log(
                f"Mechanical Ventilation Schedule set as {UmiSchedule} for "
                f"zone {zone.Name}",
                lg.DEBUG,
            )
            return umi_schedule
        else:
            # Schedule is not specified so return a constant schedule
            log(
                f"No Mechanical Ventilation Schedule specified for zone " f"{zone.Name}"
            )
            return UmiSchedule.constant_schedule(idf=zone.idf, allow_duplicates=True)

    def _set_zone_cops(self, zone, nolimit=False):
        """
        Todo:
            - Make this method zone-independent.

        Args:
            zone (Zone):
        """
        # COPs (heating and cooling)

        # Heating
        heating_meters = (
            "Heating:Electricity",
            "Heating:Gas",
            "Heating:DistrictHeating",
        )
        heating_cop = self._get_cop(
            zone,
            energy_in_list=heating_meters,
            energy_out_variable_name=(
                "Air System Total Heating Energy",
                "Zone Ideal Loads Zone Total Heating Energy",
            ),
        )
        # Cooling
        cooling_meters = (
            "Cooling:Electricity",
            "Cooling:Gas",
            "Cooling:DistrictCooling",
        )
        cooling_cop = self._get_cop(
            zone,
            energy_in_list=cooling_meters,
            energy_out_variable_name=(
                "Air System Total Cooling Energy",
                "Zone Ideal Loads Zone Total Cooling Energy",
            ),
        )

        # Capacity limits (heating and cooling)
        zone_size = zone.idf.sql()["ZoneSizes"][
            zone.idf.sql()["ZoneSizes"]["ZoneName"] == zone.Name.upper()
        ]
        # Heating
        HeatingLimitType, heating_cap, heating_flow = self._get_design_limits(
            zone, zone_size, load_name="Heating", nolimit=nolimit
        )
        # Cooling
        CoolingLimitType, cooling_cap, cooling_flow = self._get_design_limits(
            zone, zone_size, load_name="Cooling", nolimit=nolimit
        )

        self.HeatingLimitType = HeatingLimitType
        self.MaxHeatingCapacity = heating_cap
        self.MaxHeatFlow = heating_flow
        self.CoolingLimitType = CoolingLimitType
        self.MaxCoolingCapacity = cooling_cap
        self.MaxCoolFlow = cooling_flow

        self.CoolingCoeffOfPerf = cooling_cop
        self.HeatingCoeffOfPerf = heating_cop

        # If cop calc == infinity, COP = 1 because we need a value in json file.
        if heating_cop == float("infinity"):
            self.HeatingCoeffOfPerf = 1
        if cooling_cop == float("infinity"):
            self.CoolingCoeffOfPerf = 1

    def _set_thermostat_setpoints(self, zone):
        """Sets the thermostat settings and schedules for this zone.

        Thermostat Setpoints:
            - CoolingSetpoint (float):
            - HeatingSetpoint (float):
            - HeatingSchedule (UmiSchedule):
            - CoolingSchedule (UmiSchedule):

        Args:
            zone (Zone): The zone object.
        """
        # Set Thermostat set points
        # Heating and Cooling set points and schedules
        with sqlite3.connect(zone.idf.sql_file) as conn:
            sql_query = f"""
                    SELECT t.ReportVariableDataDictionaryIndex
                    FROM ReportVariableDataDictionary t
                    WHERE VariableName == 'Zone Thermostat Heating Setpoint Temperature' and KeyValue == '{zone.Name.upper()}';"""
            index = conn.execute(sql_query).fetchone()
            if index:
                sql_query = f"""
                        SELECT t.VariableValue
                        FROM ReportVariableData t
                        WHERE ReportVariableDataDictionaryIndex == {index[0]};"""
                h_array = conn.execute(sql_query).fetchall()
                if h_array:
                    heating_setpoints = np.array(h_array).flatten()
                    heating_sched = UmiSchedule.from_values(
                        Name=zone.Name + "_Heating_Schedule",
                        Values=(heating_setpoints > 0).astype(int),
                        Type="Fraction",
                        idf=zone.idf,
                        allow_duplicates=True,
                    )
                else:
                    heating_sched = None

            sql_query = f"""
                    SELECT t.ReportVariableDataDictionaryIndex
                    FROM ReportVariableDataDictionary t
                    WHERE VariableName == 'Zone Thermostat Cooling Setpoint Temperature' and KeyValue == '{zone.Name.upper()}';"""
            index = conn.execute(sql_query).fetchone()
            if index:
                sql_query = f"""
                        SELECT t.VariableValue
                        FROM ReportVariableData t
                        WHERE ReportVariableDataDictionaryIndex == {index[0]};"""
                c_array = conn.execute(sql_query).fetchall()
                if c_array:
                    cooling_setpoints = np.array(c_array).flatten()
                    cooling_sched = UmiSchedule.from_values(
                        Name=zone.Name + "_Cooling_Schedule",
                        Values=(cooling_setpoints > 0).astype(int),
                        Type="Fraction",
                        idf=zone.idf,
                        allow_duplicates=True,
                    )
                else:
                    heating_sched = None
        self.HeatingSetpoint = heating_setpoints.mean()
        self.HeatingSchedule = heating_sched
        self.CoolingSetpoint = cooling_setpoints.mean()
        self.CoolingSchedule = cooling_sched

        # If HeatingSetpoint == nan, means there is no heat or cold input,
        # therefore system is off.
        if self.HeatingSetpoint == 0:
            self.IsHeatingOn = False
        else:
            self.IsHeatingOn = True
        if self.CoolingSetpoint == 0:
            self.IsCoolingOn = False
        else:
            self.IsCoolingOn = True

    def _set_heat_recovery(self, zone):
        """Sets the heat recovery parameters for this zone.

        Heat Recovery Parameters:
            - HeatRecoveryEfficiencyLatent (float): The latent heat recovery
              effectiveness.
            - HeatRecoveryEfficiencySensible (float): The sensible heat recovery
              effectiveness.
            - HeatRecoveryType (int): None = 0, Sensible = 1 or Enthalpy = 2.
            - comment (str): A comment to append to the class comment attribute.

        Args:
            zone (Zone): The Zone object.
        """
        from itertools import chain

        # Todo: Implement loop that detects HVAC linked to Zone; than parse heat
        #  recovery. Needs to happen when a zone has a ZoneHVAC:IdealLoadsAirSystem
        # connections = zone._epbunch.getreferingobjs(
        #     iddgroups=["Zone HVAC Equipment Connections"], fields=["Zone_Name"]
        # )
        # nodes = [
        #     con.get_referenced_object("Zone_Air_Inlet_Node_or_NodeList_Name")
        #     for con in connections
        # ]
        # get possible heat recovery objects from idd
        heat_recovery_objects = zone.idf.getiddgroupdict()["Heat Recovery"]

        # get possible heat recovery objects from this idf
        heat_recovery_in_idf = list(
            chain.from_iterable(
                zone.idf.idfobjects[key.upper()] for key in heat_recovery_objects
            )
        )

        # Set defaults
        HeatRecoveryEfficiencyLatent = 0.7
        HeatRecoveryEfficiencySensible = 0.65
        HeatRecoveryType = HeatRecoveryTypes.NONE
        comment = ""

        # iterate over those objects. If the list is empty, it will simply pass.
        for object in heat_recovery_in_idf:

            if object.key.upper() == "HeatExchanger:AirToAir:FlatPlate".upper():
                # Do HeatExchanger:AirToAir:FlatPlate

                nsaot = object.Nominal_Supply_Air_Outlet_Temperature
                nsait = object.Nominal_Supply_Air_Inlet_Temperature
                n2ait = object.Nominal_Secondary_Air_Inlet_Temperature
                HeatRecoveryEfficiencySensible = (nsaot - nsait) / (n2ait - nsait)
                # Hypotheses: HeatRecoveryEfficiencySensible - 0.05
                HeatRecoveryEfficiencyLatent = HeatRecoveryEfficiencySensible - 0.05
                HeatRecoveryType = HeatRecoveryTypes.Enthalpy
                comment = (
                    "HeatRecoveryEfficiencySensible was calculated "
                    "using this formula: (Supply Air Outlet T°C -; "
                    "Supply Air Inlet T°C)/(Secondary Air Inlet T°C - "
                    "Supply Air Inlet T°C)"
                )

            elif (
                object.key.upper() == "HeatExchanger:AirToAir:SensibleAndLatent".upper()
            ):
                # Do HeatExchanger:AirToAir:SensibleAndLatent

                (
                    HeatRecoveryEfficiencyLatent,
                    HeatRecoveryEfficiencySensible,
                ) = self._get_recoverty_effectiveness(object, zone)
                HeatRecoveryType = HeatRecoveryTypes.Enthalpy

                comment = (
                    "HeatRecoveryEfficiencies were calculated using "
                    "simulation hourly values and averaged. Only values"
                    " > 0 were used in the average calculation."
                )

            elif object.key.upper() == "HeatExchanger:Desiccant:BalancedFlow".upper():
                # Do HeatExchanger:Dessicant:BalancedFlow
                # Use default values
                HeatRecoveryEfficiencyLatent = 0.7
                HeatRecoveryEfficiencySensible = 0.65
                HeatRecoveryType = HeatRecoveryTypes.Enthalpy

            elif (
                object.key.upper() == "HeatExchanger:Desiccant:BalancedFlow"
                ":PerformanceDataType1".upper()
            ):
                # This is not an actual HeatExchanger, pass
                pass
            else:
                msg = 'Heat exchanger object "{}" is not ' "implemented".format(object)
                raise NotImplementedError(msg)

        self.HeatRecoveryEfficiencyLatent = HeatRecoveryEfficiencyLatent
        self.HeatRecoveryEfficiencySensible = HeatRecoveryEfficiencySensible
        self.HeatRecoveryType = HeatRecoveryType
        self.Comments += comment

    @staticmethod
    def _get_recoverty_effectiveness(object, zone):
        """
        Args:
            object:
            zone:
        """
        rd = ReportData.from_sql_dict(zone.idf.sql())
        effectiveness = (
            rd.filter_report_data(
                name=(
                    "Heat Exchanger Sensible Effectiveness",
                    "Heat Exchanger Latent Effectiveness",
                )
            )
            .loc[lambda x: x.Value > 0]
            .groupby(["KeyValue", "Name"])
            .Value.mean()
            .unstack(level=-1)
        )
        HeatRecoveryEfficiencySensible = effectiveness.loc[
            object.Name.upper(), "Heat Exchanger Sensible Effectiveness"
        ]
        HeatRecoveryEfficiencyLatent = effectiveness.loc[
            object.Name.upper(), "Heat Exchanger Latent Effectiveness"
        ]
        return HeatRecoveryEfficiencyLatent, HeatRecoveryEfficiencySensible

    @staticmethod
    def _get_design_limits(zone, zone_size, load_name, nolimit=False):
        """Gets design limits for heating and cooling systems

        Args:
            zone (archetypal.template.zone.Zone): zone to gets information from
            zone_size (df): Dataframe from the sql EnergyPlus outpout, with the
                sizing of the heating and cooling systems
            load_name (str): 'Heating' or 'Cooling' depending on what system we
                want to characterize
        """
        if nolimit:
            return IdealSystemLimit.NoLimit, 100, 100
        try:
            cap = (
                zone_size[zone_size["LoadType"] == load_name]["UserDesLoad"].values[0]
                / zone.area
            )
            flow = (
                zone_size[zone_size["LoadType"] == load_name]["UserDesFlow"].values[0]
                / zone.area
            )
            LimitType = IdealSystemLimit.LimitFlowRateAndCapacity
        except:
            cap = 100
            flow = 100
            LimitType = IdealSystemLimit.NoLimit
        return LimitType, cap, flow

    @staticmethod
    def _get_cop(zone, energy_in_list, energy_out_variable_name):
        """Calculates COP for heating or cooling systems

        Args:
            zone (archetypal.template.zone.Zone): zone to gets information from
            energy_in_list (str or tuple): list of the energy sources for a
                system (e.g. [Heating:Electricity, Heating:Gas] for heating
                system)
            energy_out_variable_name (str or tuple): Name of the output in the
                sql for the energy given to the zone from the system (e.g. 'Air
                System Total Heating Energy')
        """
        from archetypal import ReportData

        rd = ReportData.from_sql_dict(zone.idf.sql())
        energy_out = rd.filter_report_data(name=tuple(energy_out_variable_name))
        energy_in = rd.filter_report_data(name=tuple(energy_in_list))

        # zone_to_hvac = {zone.Zone_Name: [
        #     zone.get_referenced_object(
        #         'Zone_Conditioning_Equipment_List_Name
        #         ').get_referenced_object(
        #         fieldname) for fieldname in zone.get_referenced_object(
        #         'Zone_Conditioning_Equipment_List_Name').fieldnames if
        #     zone.get_referenced_object(
        #         'Zone_Conditioning_Equipment_List_Name
        #         ').get_referenced_object(
        #         fieldname) is not None
        # ]
        #     for zone in zone.idf.idfobjects[
        #         'ZoneHVAC:EquipmentConnections'.upper()]
        # }

        outs = energy_out.groupby("KeyValue").Value.sum()
        ins = energy_in.Value.sum()

        cop = float_round(outs.sum() / ins, 3)

        return cop

    def combine(self, other, weights=None):
        """Combine two ZoneConditioning objects together.

        Args:
            other (ZoneConditioning): The other ZoneConditioning object to
                combine with.
            weights (list-like, optional): A list-like object of len 2. If None,
                the volume of the zones for which self and other belongs is
                used.

        Returns:
            (ZoneConditioning): the combined ZoneConditioning object.
        """
        # Check if other is None. Simply return self
        if not other:
            return self

        if not self:
            return other
        # Check if other is the same type as self
        if not isinstance(other, self.__class__):
            msg = "Cannot combine %s with %s" % (
                self.__class__.__name__,
                other.__class__.__name__,
            )
            raise NotImplementedError(msg)

        # Check if other is not the same as self
        if self == other:
            return self

        meta = self._get_predecessors_meta(other)

        if not weights:
            zone_weight = settings.zone_weight
            weights = [
                getattr(self._belongs_to_zone, str(zone_weight)),
                getattr(other._belongs_to_zone, str(zone_weight)),
            ]
            log(
                'using zone {} "{}" as weighting factor in "{}" '
                "combine.".format(
                    zone_weight,
                    " & ".join(list(map(str, map(int, weights)))),
                    self.__class__.__name__,
                )
            )

        a = UmiBase._float_mean(self, other, "CoolingCoeffOfPerf", weights)
        b = max(self.CoolingLimitType, other.CoolingLimitType)
        c = UmiBase._float_mean(self, other, "CoolingSetpoint", weights)
        d = max(self.EconomizerType, other.EconomizerType)
        e = UmiBase._float_mean(self, other, "HeatRecoveryEfficiencyLatent", weights)
        f = UmiBase._float_mean(self, other, "HeatRecoveryEfficiencySensible", weights)
        g = max(self.HeatRecoveryType, other.HeatRecoveryType)
        h = UmiBase._float_mean(self, other, "HeatingCoeffOfPerf", weights)
        i = max(self.HeatingLimitType, other.HeatingLimitType)
        j = UmiBase._float_mean(self, other, "HeatingSetpoint", weights)
        k = any((self.IsCoolingOn, other.IsCoolingOn))
        l = any((self.IsHeatingOn, other.IsHeatingOn))
        m = any((self.IsMechVentOn, other.IsMechVentOn))
        n = UmiBase._float_mean(self, other, "MaxCoolFlow", weights)
        o = UmiBase._float_mean(self, other, "MaxCoolingCapacity", weights)
        p = UmiBase._float_mean(self, other, "MaxHeatFlow", weights)
        q = UmiBase._float_mean(self, other, "MaxHeatingCapacity", weights)
        r = UmiBase._float_mean(self, other, "MinFreshAirPerArea", weights)
        s = UmiBase._float_mean(self, other, "MinFreshAirPerPerson", weights)
        t = UmiSchedule.combine(self.HeatingSchedule, other.HeatingSchedule, weights)
        u = UmiSchedule.combine(self.CoolingSchedule, other.CoolingSchedule, weights)
        v = UmiSchedule.combine(self.MechVentSchedule, other.MechVentSchedule, weights)

        new_attr = dict(
            CoolingCoeffOfPerf=a,
            CoolingLimitType=b,
            CoolingSetpoint=c,
            EconomizerType=d,
            HeatRecoveryEfficiencyLatent=e,
            HeatRecoveryEfficiencySensible=f,
            HeatRecoveryType=g,
            HeatingCoeffOfPerf=h,
            HeatingLimitType=i,
            HeatingSetpoint=j,
            IsCoolingOn=k,
            IsHeatingOn=l,
            IsMechVentOn=m,
            MaxCoolFlow=n,
            MaxCoolingCapacity=o,
            MaxHeatFlow=p,
            MaxHeatingCapacity=q,
            MinFreshAirPerArea=r,
            MinFreshAirPerPerson=s,
            HeatingSchedule=t,
            CoolingSchedule=u,
            MechVentSchedule=v,
        )
        # create a new object with the previous attributes
        new_obj = self.__class__(**meta, **new_attr, idf=self.idf)
        new_obj.predecessors.update(self.predecessors + other.predecessors)
        return new_obj

    def validate(self):
        """Validates UmiObjects and fills in missing values"""
        if self.HeatingSchedule is None:
            self.HeatingSchedule = UmiSchedule.constant_schedule(idf=self.idf)
        if self.CoolingSchedule is None:
            self.CoolingSchedule = UmiSchedule.constant_schedule(idf=self.idf)
        if self.MechVentSchedule is None:
            self.MechVentSchedule = UmiSchedule.constant_schedule(idf=self.idf)
        if not self.IsMechVentOn:
            self.IsMechVentOn = False
        if not self.MinFreshAirPerPerson:
            self.MinFreshAirPerPerson = 0
        if not self.MinFreshAirPerArea:
            self.MinFreshAirPerArea = 0

    def mapping(self):
        self.validate()

        return dict(
            CoolingSchedule=self.CoolingSchedule,
            CoolingCoeffOfPerf=self.CoolingCoeffOfPerf,
            CoolingSetpoint=self.CoolingSetpoint,
            CoolingLimitType=self.CoolingLimitType,
            CoolingFuelType=self.CoolingFuelType,
            EconomizerType=self.EconomizerType,
            HeatingCoeffOfPerf=self.HeatingCoeffOfPerf,
            HeatingLimitType=self.HeatingLimitType,
            HeatingFuelType=self.HeatingFuelType,
            HeatingSchedule=self.HeatingSchedule,
            HeatingSetpoint=self.HeatingSetpoint,
            HeatRecoveryEfficiencyLatent=self.HeatRecoveryEfficiencyLatent,
            HeatRecoveryEfficiencySensible=self.HeatRecoveryEfficiencySensible,
            HeatRecoveryType=self.HeatRecoveryType,
            IsCoolingOn=self.IsCoolingOn,
            IsHeatingOn=self.IsHeatingOn,
            IsMechVentOn=self.IsMechVentOn,
            MaxCoolFlow=self.MaxCoolFlow,
            MaxCoolingCapacity=self.MaxCoolingCapacity,
            MaxHeatFlow=self.MaxHeatFlow,
            MaxHeatingCapacity=self.MaxHeatingCapacity,
            MechVentSchedule=self.MechVentSchedule,
            MinFreshAirPerArea=self.MinFreshAirPerArea,
            MinFreshAirPerPerson=self.MinFreshAirPerPerson,
            Category=self.Category,
            Comments=self.Comments,
            DataSource=self.DataSource,
            Name=self.Name,
        )

    def get_ref(self, ref):
        """Gets item matching ref id

        Args:
            ref:
        """
        return next(
            iter(
                [
                    value
                    for value in ZoneConditioning.CREATED_OBJECTS
                    if value.id == ref["$ref"]
                ]
            ),
            None,
        )
