from subprocess import CalledProcessError

import pytest
from path import Path

from archetypal import (
    IDF,
    EnergyPlusProcessError,
    EnergyPlusVersion,
    EnergyPlusVersionError,
    InvalidEnergyPlusVersion,
    parallel_process,
    settings,
)
from archetypal.eplus_interface.version import get_eplus_dirs


@pytest.fixture()
def shoebox_model(config):
    """An IDF model. Yields both the idf"""
    file = "tests/input_data/umi_samples/B_Off_0.idf"
    w = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
    yield IDF(file, epw=w)


class TestIDF:
    @pytest.fixture(scope="session")
    def idf_model(self, config):
        """An IDF model. Yields both the idf"""
        file = (
            "tests/input_data/necb/NECB 2011-SmallOffice-NECB HDD "
            "Method-CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw.idf"
        )
        w = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        yield IDF(file, epw=w).simulate()

    @pytest.fixture()
    def natvent(self, config):
        """An old file that needs upgrade"""
        w = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        yield IDF(
            "tests/input_data/problematic/nat_ventilation_SAMPLE0.idf",
            epw=w,
            as_version="9-2-0",
        )

    @pytest.fixture()
    def FiveZoneNightVent1(self):
        """"""
        w = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        idfname = (
            get_eplus_dirs(settings.ep_version) / "ExampleFiles" / "5ZoneNightVent1.idf"
        )
        yield IDF(idfname, epw=w)

    @pytest.fixture()
    def natvent_v9_1_0(self, config):
        """An old file that needs upgrade"""
        w = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        yield IDF(
            "tests/input_data/problematic/nat_ventilation_SAMPLE0.idf",
            epw=w,
            as_version="9-1-0",
        )

    @pytest.fixture()
    def wont_transition_correctly(self, config):
        file = (
            "tests/input_data/problematic/RefBldgLargeOfficeNew2004_v1.4_7"
            ".2_5A_USA_IL_CHICAGO-OHARE.idf"
        )
        wf = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        yield IDF(file, epw=wf, as_version="8.9.0")

    def test_default_version_none(self):
        file = (
            "tests/input_data/necb/NECB 2011-FullServiceRestaurant-NECB HDD "
            "Method-CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw.idf"
        )
        wf = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        idf = IDF(file, epw=wf, as_version=None)
        assert idf.idf_version == EnergyPlusVersion("9-2-0")
        assert idf.idd_version == (9, 2, 0)
        assert idf.file_version == EnergyPlusVersion("9-2-0")

    def test_default_version_specified_period(self):
        file = (
            "tests/input_data/necb/NECB 2011-FullServiceRestaurant-NECB HDD "
            "Method-CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw.idf"
        )
        wf = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        idf = IDF(file, epw=wf, as_version="9.2.0")
        assert idf.idf_version == EnergyPlusVersion("9-2-0")
        assert idf.idd_version == (9, 2, 0)
        assert idf.file_version == EnergyPlusVersion("9-2-0")

    def test_default_version_specified_dash(self):
        file = (
            "tests/input_data/necb/NECB 2011-FullServiceRestaurant-NECB HDD "
            "Method-CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw.idf"
        )
        wf = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        idf = IDF(file, epw=wf, as_version="9-2-0")
        assert idf.idf_version == EnergyPlusVersion("9-2-0")
        assert idf.idd_version == (9, 2, 0)
        assert idf.file_version == EnergyPlusVersion("9-2-0")

    def test_specific_version(self, config, natvent_v9_1_0):
        assert natvent_v9_1_0.idf_version == EnergyPlusVersion("9-1-0")
        assert natvent_v9_1_0.idd_version == (9, 1, 0)
        assert natvent_v9_1_0.file_version == EnergyPlusVersion("9-1-0")

    def test_specific_version_error_simulate(self, natvent_v9_1_0):
        with pytest.raises(EnergyPlusVersionError):
            natvent_v9_1_0.simulate()

    def test_version(self, natvent_v9_1_0):
        # setting as_version
        natvent_v9_1_0.as_version = "9-2-0"
        assert natvent_v9_1_0.as_version == EnergyPlusVersion("9-2-0")

        # setting idfname
        natvent_v9_1_0.idfname = "this_name"
        assert natvent_v9_1_0.idfname == Path("this_name")

        # setting epw
        natvent_v9_1_0.epw = "newepw.epw"
        assert natvent_v9_1_0.epw == Path("newepw.epw")

        with pytest.raises(AttributeError):
            # illigal to set iddname, since it is a calculated property
            natvent_v9_1_0.iddname = "this_name"

    def test_transition_error(self, config, wont_transition_correctly):
        with pytest.raises(
            (EnergyPlusProcessError, EnergyPlusVersionError, CalledProcessError)
        ):
            assert wont_transition_correctly.simulate(ep_version="8.9.0")

    def test_sql(self, idf_model):
        assert idf_model.sql_file.exists()

    def test_processed_results(self, idf_model):
        assert idf_model.process_results()

    def test_partition_ratio(self, idf_model):
        assert idf_model.partition_ratio

    def test_space_cooling_profile(self, idf_model):
        assert not idf_model.space_cooling_profile().empty

    def test_space_heating_profile(self, idf_model):
        assert not idf_model.space_heating_profile().empty

    def test_dhw_profile(self, idf_model):
        assert not idf_model.service_water_heating_profile().empty

    def test_wwr(self, idf_model):
        assert not idf_model.wwr(round_to=10).empty

    def test_wrong_epversion(self, config):
        file = (
            "tests/input_data/problematic/RefBldgLargeOfficeNew2004_v1.4_7"
            ".2_5A_USA_IL_CHICAGO-OHARE.idf"
        )
        wf = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        with pytest.raises(InvalidEnergyPlusVersion):
            IDF(file, epw=wf, as_version="7-3-0")

    def test_parallel_process(self, config):
        w = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        files = {
            i: {"idfname": file.expand(), "epw": w}
            for i, file in enumerate(Path("tests/input_data/necb").files("*.idf")[0:3])
        }
        idfs = parallel_process(files, IDF, use_kwargs=True, processors=-1)

        assert not any(isinstance(a, Exception) for a in idfs)

    def test_load_old(self, config, natvent, FiveZoneNightVent1):
        assert natvent.idd_version == (9, 2, 0)
        assert FiveZoneNightVent1.idd_version == (9, 2, 0)

    @pytest.mark.parametrize(
        "archetype, area",
        [
            ("FullServiceRestaurant", 511),
            pytest.param(
                "Hospital",
                22422,
                marks=pytest.mark.xfail(reason="Difference cannot be explained"),
            ),
            ("LargeHotel", 11345),
            ("LargeOffice", 46320),
            ("MediumOffice", 4982),
            ("MidriseApartment", 3135),
            ("Outpatient", 3804),
            ("PrimarySchool", 6871),
            ("QuickServiceRestaurant", 232),
            ("SecondarySchool", 19592),
            ("SmallHotel", 4013),
            ("SmallOffice", 511),
            pytest.param(
                "RetailStandalone",
                2319,
                marks=pytest.mark.xfail(reason="Difference cannot be explained"),
            ),
            ("RetailStripmall", 2090),
            pytest.param(
                "Supermarket",
                4181,
                marks=pytest.mark.skip("Supermarket missing from BTAP " "database"),
            ),
            ("Warehouse", 4835),
        ],
    )
    def test_area(self, archetype, area, config):
        """Test the conditioned_area property against published values
        desired values taken from https://github.com/canmet-energy/btap"""
        import numpy as np

        idf_file = Path("tests/input_data/necb").files(f"*{archetype}*.idf")[0]
        w = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        idf = IDF(idf_file, epw=w, prep_outputs=False)

        np.testing.assert_almost_equal(
            actual=idf.net_conditioned_building_area, desired=area, decimal=0
        )


class TestMeters:
    @pytest.fixture()
    def shoebox_res(self):
        """An IDF model. Yields both the idf. This needs to be the only one used in
        the following test: test_retrieve_meters_nosim"""
        file = "tests/input_data/umi_samples/B_Res_0_WoodFrame.idf"
        w = "tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"
        yield IDF(file, epw=w)

    def test_retrieve_meters_nosim(self, config, shoebox_res):
        shoebox_res.simulation_dir.rmtree_p()
        with pytest.raises(Exception):
            print(shoebox_res.meters)

    def test_retrieve_meters(self, config, shoebox_res):
        if not shoebox_res.simulation_dir.exists():
            shoebox_res.simulate()
        shoebox_res.meters.OutputMeter.WaterSystems__MainsWater.values()


class TestThreads:
    def test_runslab(self, config):
        file = get_eplus_dirs() / "ExampleFiles" / "5ZoneAirCooledWithSlab.idf"
        epw = (
            get_eplus_dirs()
            / "WeatherData"
            / "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"
        )
        idf = IDF(file, epw, annual=False)

        assert idf.simulate()

    @pytest.mark.skip("To long to run for tests")
    def test_runbasement(self, config):
        file = get_eplus_dirs() / "ExampleFiles" / "LgOffVAVusingBasement.idf"
        epw = (
            get_eplus_dirs()
            / "WeatherData"
            / "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"
        )
        idf = IDF(file, epw, annual=False)

        assert idf.simulate()
