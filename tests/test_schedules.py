import numpy as np
import pytest

import archetypal.settings as settings
from archetypal.idfclass import IDF
from archetypal.schedule import Schedule
from archetypal.template.schedule import UmiSchedule
from archetypal.utils import config, get_eplus_dirs


@pytest.fixture()
def schedules_in_necb_specific(config):
    idf = IDF(
        "tests/input_data/necb/NECB 2011-MediumOffice-NECB HDD "
        "Method-CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw.idf"
    )

    s = Schedule(
        Name="NECB-A-Thermostat Setpoint-Heating", idf=idf, start_day_of_the_week=0
    )
    yield s


def test_plot(schedules_in_necb_specific):
    schedules_in_necb_specific.plot(
        slice=("2018/01/02", "2018/01/03"), drawstyle="steps-post"
    )


def test_plot2d(schedules_in_necb_specific):
    schedules_in_necb_specific.plot2d(show=False, save=False)


def test_make_umi_schedule(config):
    """Tests only a single schedule name"""

    idf = IDF("tests/input_data/schedules/schedules.idf")

    s = UmiSchedule(Name="CoolingCoilAvailSched", idf=idf, start_day_of_the_week=0)

    new = s.develop()

    print(len(s.all_values))
    print(len(new.all_values))
    ax = s.plot(slice=("2018/01/01 00:00", "2018/01/07"), legend=True)
    new.plot(slice=("2018/01/01 00:00", "2018/01/07"), ax=ax, legend=True)
    assert s.__class__.__name__ == "YearSchedule"
    assert len(s.all_values) == len(new.all_values)
    np.testing.assert_array_equal(new.all_values, s.all_values)


def test_constant_schedule(config):
    const = Schedule.constant_schedule()
    assert const.__class__.__name__ == "Schedule"


@pytest.fixture()
def mew_idf(config):
    yield IDF(prep_outputs=False)


def test_from_values(mew_idf):
    import numpy as np

    heating_sched = UmiSchedule.from_values(
        Name="Zone_Heating_Schedule", Values=np.ones(8760), Type="Fraction", idf=idf,
    )
    assert len(heating_sched.all_values) == 8760


idf_file = "tests/input_data/schedules/test_multizone_EP.idf"


def schedules_idf():
    config(cache_folder="tests/.temp/cache")
    idf = IDF(
        idf_file,
        epw="tests/input_data/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw",
        readvars=True,
        include=[
            get_eplus_dirs(settings.ep_version)
            / "DataSets"
            / "TDV"
            / "TDV_2008_kBtu_CTZ06.csv"
        ],
    )
    return idf


idf = schedules_idf()
schedules = list(idf.get_all_schedules(yearly_only=True).keys())
ids = [i.replace(" ", "_") for i in schedules]


@pytest.fixture(scope="module")
def csv_out(config):
    idf = schedules_idf().simulate()
    csv = idf.simulation_dir.files("*out.csv")[0]
    yield csv


schedules = [
    pytest.param(
        schedule,
        marks=pytest.mark.xfail(
            reason="Can't quite capture all possibilities with special days"
        ),
    )
    if schedule == "POFF"
    else pytest.param(schedule, marks=pytest.mark.xfail(raises=NotImplementedError))
    if schedule == "Cooling Setpoint Schedule"
    else schedule
    for schedule in schedules
]


@pytest.fixture(params=schedules, ids=ids, scope="module")
def schedule_parametrized(request, csv_out):
    """Create the test_data"""
    import pandas as pd

    # read original schedule
    idf = schedules_idf()
    schName = request.param
    orig = Schedule(Name=schName, idf=idf)

    print(
        "{name}\tType:{type}\t[{len}]\tValues:{"
        "values}".format(
            name=orig.Name,
            type=orig.schType,
            values=orig.all_values,
            len=len(orig.all_values),
        )
    )

    # create year:week:day version
    new_eps = orig.to_year_week_day()
    new = orig

    index = orig.series.index
    epv = pd.read_csv(csv_out)
    epv.columns = epv.columns.str.strip()
    epv = epv.loc[:, schName.upper() + ":Schedule Value [](Hourly)"].values
    expected = pd.Series(epv, index=index)

    print("Year: {}".format(new_eps[0].Name))
    print("Weeks: {}".format([obj.Name for obj in new_eps[1]]))
    print("Days: {}".format([obj.Name for obj in new_eps[2]]))

    yield orig, new, expected


def test_ep_versus_schedule(schedule_parametrized):
    """Main test. Will run the idf using EnergyPlus, retrieve the csv file,
    create the schedules and compare"""

    orig, new, expected = schedule_parametrized

    # slice_ = ('2018/01/01 00:00', '2018/01/08 00:00')  # first week
    # slice_ = ('2018/05/20 12:00', '2018/05/22 12:00')
    slice_ = ("2018-10-05 12:00", "2018-10-07 12:00")  # Holiday
    # slice_ = ('2018/01/01 00:00', '2018/12/31 23:00')  # all year
    # slice_ = ('2018/04/30 12:00', '2018/05/01 12:00')  # break

    mask = expected.values.round(3) != orig.all_values.round(3)

    # # region Plot
    # fig, ax = plt.subplots(1, 1, figsize=(5, 4))
    # orig.plot(slice=slice_, ax=ax, legend=True, drawstyle='steps-post',
    #           linestyle='dashed')
    # new.plot(slice=slice_, ax=ax, legend=True, drawstyle='steps-post',
    #          linestyle='dotted')
    # expected.loc[slice_[0]:slice_[1]].plot(label='E+', legend=True, ax=ax,
    #                                        drawstyle='steps-post',
    #                                        linestyle='dashdot')
    # ax.set_title(orig.Name.capitalize())
    # plt.show()
    # # endregion

    print(orig.series[mask])
    assert (
        orig.all_values.round(3)[0 : 52 * 7 * 24] == expected.round(3)[0 : 52 * 7 * 24]
    ).all()
    assert (
        new.all_values.round(3)[0 : 52 * 7 * 24] == expected.round(3)[0 : 52 * 7 * 24]
    ).all()
