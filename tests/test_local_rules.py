"""City jobsite rules: construction hours, local jobsite permits and deadlines, C&D debris and San Diego fees.

Expected values are worked by hand from the rule text in research/web_local-jobsite.verified.json (the arithmetic is
in the comments), never read back from the module.
"""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, local_rules as lr

AS_OF = "2026-11-25"           # Wednesday; Thanksgiving (a San Diego City holiday) is tomorrow


def ids(result):
    return {c.rule_id for c in result.citations}


def req(result, prefix):
    return next(r for r in result.requirements if r.item.startswith(prefix))


def deadline(result, prefix):
    return next(d for d in result.deadlines if d.what.startswith(prefix))


def fee(result, prefix):
    return next(f for f in result.fees if f.item.startswith(prefix))


# ---------------------------------------------------------------- rulebook wiring
def test_rules_exist_and_every_pack_rule_is_used_or_explained():
    book = {r.id: r for r in core.all_rules()}
    assert set(lr.RULE_IDS) <= set(book)
    pack = {r.id for r in book.values() if r.pack == "web_local-jobsite"}
    assert len(pack) == 130
    local_used = {i for i in lr.RULE_IDS if i.startswith("local-")}
    assert local_used | set(lr.NOT_USED) == pack and not local_used & set(lr.NOT_USED)
    assert {lr.CALGREEN, lr.USA811, lr.NESHAP} <= set(lr.RULE_IDS)
    assert all(book[i].verified for i in (lr.CALGREEN, lr.USA811, lr.NESHAP))
    # San Diego and Los Angeles rules are verified except the five LA search-excerpt rules; the other six cities
    # and the two Vehicle Code rules are all unverifiable.
    la_unverified = {lr.LA_500FT_U, lr.LA_AFTER_HOURS_U, lr.LA_EQUIP_U, lr.LA_BOE_U, lr.LA_PODS_U}
    for i in pack:
        if i.startswith(("local-sd-", "local-la-")) and i not in la_unverified:
            assert book[i].verified, i
        else:
            assert book[i].status == "unverifiable", i
    assert book[lr.SD_TCP_FEES].status == "corrected" and book[lr.SD_HOLIDAYS].status == "corrected"


@pytest.mark.parametrize("raw,key", [
    ("SD", "san_diego"), ("  city of san diego ", "san_diego"), ("San Diego, CA", "san_diego"), ("S.D.", "san_diego"),
    ("The City of San Diego", "san_diego"), ("L.A.", "los_angeles"), ("Los Angeles, California", "los_angeles"),
    ("City of LA", "los_angeles"), ("San José", "san_jose"), ("sacto", "sacramento"), ("LONG BEACH", "long_beach"),
    ("SF", "san_francisco"), ("Fresno", "fresno"), ("oakland ca", "oakland"),
])
def test_city_aliases(raw, key):
    assert lr.normalize_city(raw) == key


@pytest.mark.parametrize("raw,msg", [
    ("San Diego County", "is a county"), ("unincorporated LA County", "Los Angeles County"),
    ("Irvine", "No city jobsite rules for 'Irvine'"), ("San Deigo", "Did you mean San Diego"), ("", "city is required"),
])
def test_city_errors(raw, msg):
    with pytest.raises(ValueError, match=msg):
        lr.normalize_city(raw)


# ---------------------------------------------------------------- construction hours: San Diego
def test_sd_weekday_daytime_is_allowed_and_the_window_is_7_to_7():
    # SDMC 59.5.0404(a): no noisy work 7 p.m.-7 a.m.; DSD: no noise permit 7 a.m.-7 p.m. Monday-Saturday.
    r = lr.construction_hours("SD", "2026-12-01T10:00", as_of=AS_OF)      # Tuesday
    assert r.status == "allowed" and r.allowed is True and r.allowed_windows == ["07:00-19:00"]
    assert r.city == "San Diego" and r.city_rules_verified and r.verified is True
    assert r.holiday is False and r.holiday_basis == "City of San Diego 2026 holiday list"
    assert {lr.SD_BAN, lr.SD_WINDOWS, lr.SD_75DB, lr.SD_HOLIDAYS} <= ids(r)
    assert "75 dB" in r.next_step and r.penalty is None and r.requirements == []


@pytest.mark.parametrize("hhmm,status", [("06:59", "needs_permit"), ("07:00", "allowed"), ("18:59", "allowed"),
                                         ("19:00", "needs_permit"), ("23:30", "needs_permit")])
def test_sd_boundaries(hhmm, status):
    assert lr.construction_hours("San Diego", f"2026-12-01T{hhmm}", as_of=AS_OF).status == status


def test_sd_sunday_needs_a_noise_permit_all_day():
    r = lr.construction_hours("sd", "2026-11-29T10:00", as_of=AS_OF)
    assert r.status == "needs_permit" and r.allowed is False and r.allowed_windows == []
    assert [(w.start, w.end) for w in r.day_schedule] == [("00:00", "24:00")]
    assert r.next_allowed_start == "2026-11-30T07:00"                      # Monday 7 a.m.
    # $250 civil penalty, up to $2,500 per incident (NCC policy flyer)
    assert "$250.00 civil penalty" in r.penalty and "$2,500.00 per incident" in r.penalty
    assert lr.SD_PENALTY in ids(r) and r.verified is True


def test_sd_thanksgiving_2026_is_a_city_holiday():
    # City Holidays 2026: "Thursday, Nov. 26 Thanksgiving Day"; the Friday after is not on the list.
    r = lr.construction_hours("City of San Diego", "2026-11-26T10:00", as_of=AS_OF)
    assert r.holiday is True and r.holiday_name == "Thanksgiving Day" and r.status == "needs_permit"
    assert r.next_allowed_start == "2026-11-27T07:00"
    # The permit route is dated: neighbor notice 72 h before 2026-11-26 10:00 = 2026-11-23 10:00 (already passed).
    n = deadline(r, "Deliver the neighbor notice")
    assert (n.due, n.due_time, n.status) == ("2026-11-23", "10:00", "passed")
    assert "already late" in r.next_step and "NoisePermits@sandiego.gov" in r.next_step
    assert req(r, "Construction Noise Permit").detail.count("$529.71") == 1


def test_sd_holiday_list_wins_over_the_caller():
    r = lr.construction_hours("SD", "2026-11-26T10:00", is_holiday=False, as_of=AS_OF)
    assert r.holiday is True and r.status == "needs_permit"
    assert any("City list is used" in n for n in r.notes)


def test_sd_presidents_day_is_treated_as_a_holiday_with_the_washington_birthday_caveat():
    # 2026 list: "Monday, Feb. 16 Presidents' Day"; SDMC 59.5.0404(a) excepts Washington's Birthday, but the City
    # does not say whether Presidents' Day is that day, and DSD requires the permit all day on City holidays.
    r = lr.construction_hours("SD", "2026-02-16T10:00", as_of="2026-02-01")
    assert r.status == "needs_permit" and r.holiday_name == "Presidents' Day"
    assert any("Washington's Birthday" in n and "Confirm" in n for n in r.notes)


def test_sd_saturday_daytime_is_allowed_but_the_dsd_headline_conflict_is_flagged():
    r = lr.construction_hours("SD", "2026-11-28T10:00", as_of=AS_OF)
    assert r.status == "allowed"
    assert any("all day on Saturdays" in n for n in r.notes)


def test_sd_2027_dates_depend_on_the_unpublished_holiday_list():
    # list_for_2027_published: false -> a 2027 weekday daytime answer depends on whether it is a City holiday.
    r = lr.construction_hours("SD", "2027-01-18T09:00", as_of=AS_OF)
    assert r.status == "check_holiday" and r.allowed is None and r.holiday is None
    assert "7 a.m.-7 p.m. if not a holiday" in r.summary and "is_holiday=true or false" in r.next_step
    assert lr.construction_hours("SD", "2027-01-18T09:00", is_holiday=False, as_of=AS_OF).status == "allowed"
    assert lr.construction_hours("SD", "2027-01-18T09:00", is_holiday="yes", as_of=AS_OF).status == "needs_permit"
    # Night is a permit either way, so no holiday question there.
    assert lr.construction_hours("SD", "2027-01-18T21:00", as_of=AS_OF).status == "needs_permit"


def test_sd_night_street_work_dates_the_tcp_and_noise_permit_steps():
    # Tuesday 2026-12-08 22:00 is standard night work (Sun-Thu nights 9 p.m.-5 a.m., IB 177) -> TCP night hours AND
    # a Construction Noise Permit.
    # Noise permit (DS-269 item 5): 5 working days before 12/08 = 12/07, 12/04, 12/03, 12/02, 12/01 -> 2026-12-01.
    # TCP first, ~15 business days before 12/01, skipping 11/26 Thanksgiving and 11/11 Veterans Day:
    #   11/30 11/27 11/25 11/24 11/23 11/20 11/19 11/18 11/17 11/16 11/13 11/12 11/10 11/09 11/06 -> 2026-11-06.
    # Neighbor notice: 2026-12-08 22:00 - 72 h = 2026-12-05 22:00.
    r = lr.construction_hours("SD", "2026-12-08T22:00", work_type="lane closure", as_of=AS_OF)
    assert r.work_type == "street_work" and r.status == "needs_permit"
    assert "standard night work" in r.summary
    np_ = deadline(r, "Submit the Construction Noise Permit application")
    assert (np_.due, np_.status, np_.estimate) == ("2026-12-01", "upcoming", False)
    tcp = deadline(r, "Get the Traffic Control Permit approved first")
    assert (tcp.due, tcp.status, tcp.estimate) == ("2026-11-06", "passed", True)
    n = deadline(r, "Deliver the neighbor notice")
    assert (n.due, n.due_time) == ("2026-12-05", "22:00")
    assert "behind DSD's processing estimate" in r.next_step and "2026-12-01" in r.next_step
    assert any("skipped: 2026-11-11 Veterans Day, 2026-11-26 Thanksgiving Day" in m for m in r.math)
    assert {lr.SD_NP_TCP_FIRST, lr.SD_NP_5WD, lr.SD_TCP_15BD, lr.SD_NP_NOTICE, lr.SD_NP_DS3073,
            lr.SD_TCP_NO_PERMIT, lr.SD_PENALTY} <= ids(r)
    assert r.verified is True


def test_sd_street_work_in_the_afternoon_needs_tcp_approval_not_a_noise_permit():
    # 4 p.m. Tuesday: outside 8:30-3:30 standard day work, but inside 7 a.m.-7 p.m. (no noise permit).
    r = lr.construction_hours("SD", "2026-12-01T16:00", work_type="street_work", as_of=AS_OF)
    assert r.status == "needs_permit" and r.allowed_windows == ["08:30-15:30"]
    assert "approved on your Traffic Control Permit" in r.next_step and "No noise permit" in r.next_step
    assert "stop-work order" in r.penalty and "$250" not in r.penalty
    assert lr.SD_NP_FEE not in ids(r)
    assert lr.construction_hours("SD", "2026-12-01T10:00", work_type="sidewalk", as_of=AS_OF).status == "allowed"
    # Saturday: no right-of-way work unless approved (DS-269 item 4)
    sat = lr.construction_hours("SD", "2026-12-05T10:00", work_type="street work", as_of=AS_OF)
    assert sat.status == "needs_permit" and sat.allowed_windows == []


def test_sd_deliveries_and_bin_swaps():
    d = lr.construction_hours("SD", "2026-12-01T05:30", work_type="concrete delivery", as_of=AS_OF)
    assert d.work_type == "delivery" and d.status == "restricted" and d.allowed is None
    assert "before 7 a.m." in d.summary and lr.SD_DELIVERY in ids(d)
    assert d.next_step.endswith("2026-12-01 07:00.")
    # SDMC 59.5.0406: refuse collection vehicles not 7 p.m.-6 a.m. in residential areas without a permit.
    b = lr.construction_hours("SD", "2026-12-01T05:30", work_type="dumpster swap", as_of=AS_OF)
    assert b.work_type == "bin_service" and b.status == "needs_permit" and b.allowed_windows == ["06:00-19:00"]
    assert b.next_allowed_start == "2026-12-01T06:00" and b.penalty is None
    assert any("roll-off" in n for n in b.notes)
    assert lr.construction_hours("SD", "2026-12-01T05:30", work_type="bin_service", residential_area=False,
                                 as_of=AS_OF).status == "allowed"


def test_sd_emergency_work_48_hour_notice():
    # SDMC 59.5.0404(c): notify within 48 hours after the emergency work starts: 2026-12-01 10:00 + 48 h.
    r = lr.construction_hours("SD", "2026-12-01T10:00", emergency_work=True, as_of=AS_OF)
    e = deadline(r, "Notify the Noise Abatement Administrator")
    assert (e.due, e.due_time) == ("2026-12-03", "10:00") and lr.SD_EMERG in ids(r)


def test_sd_date_only_and_time_formats():
    r = lr.construction_hours("SD", "2026-11-29", as_of=AS_OF)              # Sunday, date only
    assert r.time is None and r.status == "needs_permit"
    n = deadline(r, "Deliver the neighbor notice")                         # 72 h with no start time -> 3 days
    assert n.due == "2026-11-26" and n.due_time is None
    pm = lr.construction_hours("SD", "2026-12-01 6:30 pm", as_of=AS_OF)
    assert pm.time == "18:30" and pm.status == "allowed"
    utc = lr.construction_hours("SD", "2026-12-01T15:30Z", as_of=AS_OF)    # 07:30 Pacific (PST, UTC-8)
    assert utc.time == "07:30" and utc.status == "allowed"


# ---------------------------------------------------------------- construction hours: Los Angeles
@pytest.mark.parametrize("when,status", [
    ("2026-12-01T06:59", "not_allowed"), ("2026-12-01T07:00", "allowed"), ("2026-12-01T20:59", "allowed"),
    ("2026-12-01T21:00", "not_allowed"),                                     # Mon-Fri 7 a.m.-9 p.m.
    ("2026-11-28T07:59", "not_allowed"), ("2026-11-28T08:00", "allowed"), ("2026-11-28T18:00", "not_allowed"),
    ("2026-11-29T12:00", "not_allowed"),                                     # Saturday 8-6; Sunday none
])
def test_la_citywide_hours(when, status):
    assert lr.construction_hours("LA", when, is_holiday=False, as_of=AS_OF).status == status


def test_la_sunday_is_not_allowed_with_the_misdemeanor_penalty():
    r = lr.construction_hours("Los Angeles", "2026-11-29T10:00", as_of=AS_OF)
    assert r.status == "not_allowed" and r.next_allowed_start == "2026-11-30T07:00"
    # LADBS: misdemeanor; 2017 notice: not more than $1,000 and/or not more than six months in County Jail.
    assert "up to $1,000.00" in r.penalty and "6 months" in r.penalty
    assert r.verified is True and lr.LA_AFTER_HOURS_U not in ids(r)
    assert "no verified after-hours permission route" in r.next_step


def test_la_national_holiday_is_unknown_unless_told():
    # LADBS Good Neighbor sheet: "Saturday or National Holiday 8 AM - 6 PM" (no list of the holidays).
    r = lr.construction_hours("L.A.", "2026-12-25T07:30", as_of=AS_OF)
    assert r.status == "check_holiday" and r.holiday is None
    assert [w.status for w in r.day_schedule] == ["not_allowed", "check_holiday", "allowed", "check_holiday",
                                                  "not_allowed"]
    assert lr.construction_hours("LA", "2026-12-25T07:30", is_holiday=True, as_of=AS_OF).status == "not_allowed"
    assert lr.construction_hours("LA", "2026-12-25T07:30", is_holiday=False, as_of=AS_OF).status == "allowed"
    assert lr.construction_hours("LA", "2026-12-25T10:00", as_of=AS_OF).status == "allowed"     # same either way


def test_la_hcr_hillside_district():
    # HCR: weekdays 8 a.m.-6 p.m.; Saturdays interior work only; no floodlights after 6 p.m.
    assert lr.construction_hours("LA", "2026-12-01T07:30", hcr_district=True, is_holiday=False,
                                 as_of=AS_OF).status == "not_allowed"
    wk = lr.construction_hours("LA", "2026-12-01T17:30", hcr_district=True, is_holiday=False, as_of=AS_OF)
    assert wk.status == "allowed" and wk.allowed_windows == ["08:00-18:00"]
    assert any("floodlights" in n for n in wk.notes) and lr.LA_HCR_LIGHTS in ids(wk)
    sat = lr.construction_hours("LA", "2026-11-28T09:00", hcr_district=True, is_holiday=False, as_of=AS_OF)
    assert sat.status == "not_allowed" and "only interior work" in sat.summary
    assert sat.next_allowed_start == "2026-11-30T08:00"
    inside = lr.construction_hours("LA", "2026-11-28T09:00", work_type="interior work", hcr_district=True,
                                   is_holiday=False, as_of=AS_OF)
    assert inside.status == "allowed" and inside.verified is True


def test_la_deliveries_lean_on_an_unverified_rule():
    r = lr.construction_hours("LA", "2026-12-01T10:00", work_type="delivery", is_holiday=False, as_of=AS_OF)
    assert r.status == "allowed" and r.verified is False and r.unverified_rule_ids == [lr.LA_500FT_U]
    assert any(n.startswith("NOT VERIFIED (unverifiable)") for n in r.notes)


# ---------------------------------------------------------------- construction hours: unverified cities
@pytest.mark.parametrize("city,when,status", [
    ("San Francisco", "2026-12-01T21:00", "needs_permit"),   # 8 p.m.-7 a.m. > ambient + 5 dBA needs a permit
    ("San Francisco", "2026-12-01T19:59", "allowed"),
    ("San Jose", "2026-12-05T10:00", "not_allowed"),          # weekday 7-7 only
    ("Sacramento", "2026-12-06T08:00", "restricted"),         # Sunday exempt 9-6 only
    ("Sacramento", "2026-12-05T07:00", "allowed"),            # Mon-Sat 7-6
    ("Oakland", "2026-12-05T10:00", "allowed"),               # COA Saturday 9-5
    ("Oakland", "2026-12-05T17:00", "restricted"),
    ("Long Beach", "2026-12-06T10:00", "needs_permit"),       # Sunday needs a permit
    ("Long Beach", "2026-12-05T08:59", "needs_permit"),       # Saturday 9-6
    ("Fresno", "2026-12-01T21:59", "allowed"),                # 7 a.m.-10 p.m. Mon-Sat
    ("Fresno", "2026-12-06T10:00", "restricted"),             # Sunday not exempt
])
def test_unverified_city_hours(city, when, status):
    r = lr.construction_hours(city, when, as_of=AS_OF)
    assert r.status == status and r.verified is False and r.city_rules_verified is False
    assert r.unverified_rule_ids


@pytest.mark.parametrize("kwargs,msg", [
    ({"city": "SD", "when": "12/01/2026 10am"}, "ISO date"),
    ({"city": "SD", "when": "2026-12-01T25:00"}, "impossible time"),
    ({"city": "SD", "when": "2026-02-30T10:00"}, "impossible date"),
    ({"city": "SD", "when": "2026-12-01 13:00 pm"}, "1-12"),
    ({"city": "SD", "when": "2026-12-01T10:00", "work_type": "skydiving"}, "work_type must be"),
    ({"city": "SD", "when": "2026-12-01T10:00", "hcr_district": True}, "only to Los Angeles"),
    ({"city": "LA", "when": "2026-12-01T10:00", "work_type": "bin service"}, "only for San Diego"),
    ({"city": "LA", "when": "2026-12-01T10:00", "emergency_work": True}, "San Diego"),
    ({"city": "SD", "when": "2026-12-01T10:00", "is_holiday": "maybe"}, "true or false"),
    ({"city": "Orange County", "when": "2026-12-01T10:00"}, "is a county"),
])
def test_construction_hours_rejects_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        lr.construction_hours(**kwargs, as_of=AS_OF)


# ---------------------------------------------------------------- local jobsite requirements
def test_sd_street_job_with_dumpster_night_work_and_trench():
    # Start Monday 2026-12-14 07:00, 2 blocks for 5 weeks.
    r = lr.local_jobsite_requirements(
        "San Diego", "2026-12-14T07:00", work_in_public_right_of_way=True, uses_street_for_bin=True,
        night_sunday_or_holiday_work=True, trenching_in_street=True, blocks=2, weeks_in_street=5, as_of=AS_OF)
    assert r.verified is True and r.city_rules_verified
    assert r.triggers == ["work in the public right-of-way", "trenching in a street", "dumpster in the street",
                          "night, Sunday or holiday work"]
    due = {d.what: (d.due, d.due_time, d.status, d.estimate) for d in r.deadlines}
    # TCP legal minimum (DS-269 item 19): 5 working days before 12/14 = 12/11 12/10 12/09 12/08 12/07 -> 12/07
    assert due["Traffic Control Permit (DSD)"] == ("2026-12-07", None, "upcoming", False)
    # ~15 business days: ... 11/27, (11/26 Thanksgiving skipped) 11/25 11/24 11/23 11/20 -> 2026-11-20 (estimate)
    assert due["Submit the TCP early (processing time)"] == ("2026-11-20", None, "passed", True)
    assert due["Notify agencies"][0] == "2026-12-07"                       # 5 working days (IB 177 VII)
    assert due["Post Tow-Away/No Parking signs"][:2] == ("2026-12-11", "07:00")   # 72 h before 12/14 07:00
    # dumpster (IB 718): 3 working days before placement = 12/11 12/10 12/09
    assert due["Traffic Control Permit for the dumpster"][0] == "2026-12-09"
    # noise permit: 5 working days -> 12/07; TCP-first 15 business days before 12/07 -> 2026-11-13 (estimate)
    assert due["Submit the Construction Noise Permit application"][0] == "2026-12-07"
    assert due["Get the Traffic Control Permit approved first"] == ("2026-11-13", None, "passed", True)
    assert due["Deliver the neighbor notice"][:2] == ("2026-12-11", "07:00")
    assert due["Call 811 (Underground Service Alert)"][0] == "2026-12-10"   # 2 working days: 12/11, 12/10
    # TCP extension: 12/14 + 3 weeks = 2027-01-04; 5 working days back counts 2027-01-01 (no 2027 City list):
    # 01/01 12/31 12/30 12/29 12/28 -> 2026-12-28, with the caveat that a 2027 holiday makes it earlier.
    assert due["Extend the Traffic Control Permit"][0] == "2026-12-28"
    assert any("no City holiday list is published for 2027" in m for m in r.math)
    # Fees (IB 177 / IB 718):
    #   initial 2 x $131.52 = $263.04; extension ceil(5 - 3) = 2 weeks x 2 blocks x $87.68 = $350.72;
    #   processing (1 initial + 1 extension request) x $87.68 = $175.36; dumpster ceil(5/4) = 2 x $176.57 = $353.14
    #   + $87.68 processing; noise permit $529.71. Total $1,759.65.
    assert [(f.item, f.amount_usd) for f in r.fees] == [
        ("Traffic Control Initial", 263.04), ("Traffic Control Extension", 350.72),
        ("Traffic Control Processing Fee", 175.36), ("Dumpster fee", 353.14),
        ("Processing fee (dumpster / container permit)", 87.68), ("Construction Noise Permit", 529.71)]
    assert r.fees_total_usd == 1759.65
    assert r.next_step.startswith("Behind the city's processing estimate") and "2026-12-07" in r.next_step
    c = req(r, "Placement").detail                                          # IB 718 clearances
    assert "10 ft" in c and "25 ft" in c and "15 ft" in c and "red (no parking)" in c
    assert "$1,000,000.00" in req(r, "Insurance").detail


def test_sd_private_property_night_work_before_thanksgiving():
    # Work Friday 2026-11-27, no right-of-way. The Thanksgiving closure (11/26) is inside the 5-working-day window
    # (11/25 11/24 11/23 11/20 11/19 -> 11/19), so file 3 business days before it: 11/25 11/24 11/23 -> 11/23.
    r = lr.local_jobsite_requirements("SD", "2026-11-27", night_sunday_or_holiday_work=True, as_of="2026-11-20")
    h = deadline(r, "File before the Thanksgiving Day closure")
    assert (h.due, h.days_left) == ("2026-11-23", 3)
    n = deadline(r, "Deliver the neighbor notice")                         # no time: 3 days before
    assert n.due == "2026-11-24" and n.due_time is None
    assert "Apply early (work on private property)" in [x.item for x in r.requirements]
    assert r.fees_total_usd == 529.71 and r.verified is True


def test_sd_right_of_way_construction_items():
    r = lr.local_jobsite_requirements("SD", "2026-12-14", work_in_public_right_of_way=True,
                                      builds_in_right_of_way=True, affects_traffic_signals=True,
                                      disturbs_survey_monument=True, private_improvement_stays_in_row=True,
                                      as_of=AS_OF)
    items = [x.item for x in r.requirements]
    assert "Public Right-of-Way Permit (DSD)" in items and "Encroachment Maintenance and Removal Agreement" in items
    assert deadline(r, "Traffic signals").due == "2026-12-07"               # 5 working days
    assert deadline(r, "Survey monument notice").due == "2026-12-07"        # 7 calendar days before 12/14
    assert "two extensions" in req(r, "ROW permit term").detail
    assert r.fees == [] and r.fees_total_usd is None


def test_la_hillside_haul_route_and_demolition():
    # 2,500 cy in a Hillside Grading Area (> 1,000 cy -> BBSC hearing).
    # Fee (GGI-08): $529 + $100 x ceil((2,500 - 1,000) / 1,000) = $529 + $200 = $729; x 1.22 = $889.38; + $10.
    r = lr.local_jobsite_requirements("LA", "2027-03-01", earthwork_cubic_yards=2500, hillside_grading_area=True,
                                      demolition=True, building_age_years=60, contractor_license_classes=["B"],
                                      as_of=AS_OF)
    assert fee(r, "Haul route").amount_usd == 899.38 and r.fees_total_usd == 899.38
    assert deadline(r, "Haul route approval").due == "2027-01-04"           # 8 weeks before 2027-03-01
    # NESHAP: 10 working days, Mon-Fri including holidays: 2/26 ... 2/15 -> 2027-02-15
    assert deadline(r, "Asbestos/demolition notice").due == "2027-02-15"
    assert deadline(r, "Demolition inspection request").due == "2027-02-28"  # 24 h before, date only
    assert "45-year demolition notice" in [x.item for x in r.requirements]  # 60 > 45
    assert "are not C-21 or A" in req(r, "Who may pull").detail
    assert any("22% surcharge" in n for n in r.notes)
    assert r.verified is True and {lr.LA_HAUL_HEARING, lr.LA_HAUL_FEE, lr.LA_DEMO_45, lr.NESHAP} <= ids(r)


@pytest.mark.parametrize("cy,expected", [
    (1000, None),            # "more than 1,000 cubic yards": exactly 1,000 needs no hearing
    (1001, 777.38),          # ($529 + $100 x 1) x 1.22 + $10 = $767.38 + $10
    (2000, 777.38),          # ceil(1,000 / 1,000) = 1 step
    (2001, 899.38),          # ceil(1,001 / 1,000) = 2 steps
])
def test_la_haul_route_fee_steps(cy, expected):
    r = lr.local_jobsite_requirements("LA", "2027-03-01", earthwork_cubic_yards=cy, hillside_grading_area=True,
                                      as_of=AS_OF)
    assert (r.fees_total_usd if r.fees else None) == expected


def test_la_eo1_and_non_hillside_and_age_boundary():
    eo1 = lr.local_jobsite_requirements("LA", "2027-03-01", earthwork_cubic_yards=2500, hillside_grading_area=True,
                                        eo1_wildfire_rebuild=True, as_of=AS_OF)
    assert "Haul route (EO 1 wildfire rebuild): ministerial" in [x.item for x in eo1.requirements]
    assert eo1.fees == [] and "Palisades" in req(eo1, "Haul route (EO 1").detail
    flat = lr.local_jobsite_requirements("LA", "2027-03-01", earthwork_cubic_yards=2500, as_of=AS_OF)
    assert flat.fees == [] and any("Hillside Grading Area" in n for n in flat.notes)
    young = lr.local_jobsite_requirements("LA", None, demolition=True, building_age_years=45, as_of=AS_OF)
    assert not [x for x in young.requirements if "45-year" in x.item]       # notice is for "over 45 years"
    old = lr.local_jobsite_requirements("LA", None, demolition=True, building_age_years=46,
                                        contractor_license_classes="C-21", as_of=AS_OF)
    assert [x for x in old.requirements if "45-year" in x.item] and "qualify" in req(old, "Who may pull").detail
    assert old.next_step.startswith("Give start_date")


def test_la_street_bin_and_night_work():
    r = lr.local_jobsite_requirements("LA", "2026-12-14", uses_street_for_bin=True, bin_type="PODS container",
                                      night_sunday_or_holiday_work=True, as_of=AS_OF)
    assert r.verified is False
    assert set(r.unverified_rule_ids) == {lr.LA_PODS_U, lr.LA_AFTER_HOURS_U}
    d = lr.local_jobsite_requirements("LA", "2026-12-14", uses_street_for_bin=True, as_of=AS_OF)
    assert d.verified is True and any("roll-off dumpster" in n for n in d.notes)


def test_unverified_city_requirements():
    r = lr.local_jobsite_requirements("San Francisco", "2026-12-14", uses_street_for_bin=True, as_of=AS_OF)
    assert r.verified is False and "UNVERIFIED" in r.next_step
    # 10 business days before 12/14: 12/11 12/10 12/09 12/08 12/07 12/04 12/03 12/02 12/01 11/30
    assert deadline(r, "Debris box in the street").due == "2026-11-30"
    lb = lr.local_jobsite_requirements("Long Beach", "2026-12-14", uses_street_for_bin=True, as_of=AS_OF)
    assert deadline(lb, "Temporary Occupancy Permit").due == "2026-12-09"   # 3 business days: 12/11 12/10 12/09


@pytest.mark.parametrize("kwargs,msg", [
    ({"city": "SD", "blocks": 2}, "set work_in_public_right_of_way"),
    ({"city": "LA", "blocks": 2, "work_in_public_right_of_way": True}, "only for San Diego"),
    ({"city": "SD", "hillside_grading_area": True}, "Los Angeles designations"),
    ({"city": "SD", "earthwork_cubic_yards": -5}, "cannot be below 0"),
    ({"city": "SD", "uses_street_for_bin": True, "bin_type": "crane"}, "bin_type must be"),
    ({"city": "SD", "affects_traffic_signals": True}, "need work_in_public_right_of_way"),
    ({"city": "SD", "start_date": "next Tuesday"}, "start_date must be an ISO date"),
    ({"city": "SD", "work_in_public_right_of_way": "sometimes"}, "true or false"),
])
def test_jobsite_rejects_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        lr.local_jobsite_requirements(**kwargs, as_of=AS_OF)


# ---------------------------------------------------------------- C&D debris
def test_sd_new_home_deposit_diversion_refund_and_landfill_cost():
    # ESD table: residential new construction $0.40/sq ft: 2,400 x $0.40 = $960.
    # 22 of 40 t diverted = 55% < 65% -> short 40 x 0.65 - 22 = 26 - 22 = 4 t; refund pro-rated (formula unpublished).
    # Refund request within 180 days of final inspection 2026-10-01 -> 2027-03-30.
    # Miramar FY2027 C&D, City waste $241/t: (40 - 22) = 18 t x $241 = $4,338.
    r = lr.construction_debris_plan("San Diego", "new home", 2400, total_debris_tons=40, diverted_tons=22,
                                    final_inspection_date="2026-10-01", as_of=AS_OF)
    assert r.project_type == "residential_new_construction" and r.deposit_usd == 960.0
    assert r.diversion_required_percent == 65 and r.diversion_achieved_percent == 55.0
    assert r.tons_to_divert_for_target == 26.0 and r.additional_tons_needed == 4.0
    assert r.refund_outlook == "pro_rated" and r.refund_deadline == "2027-03-30"
    assert r.landfill_cost_usd == 4338.0
    assert deadline(r, "Request the refund").days_left == 125               # 2026-11-25 -> 2027-03-30
    assert any("does not publish the formula" in n for n in r.notes)
    assert "project address" in req(r, "Use certified facilities").detail
    assert r.verified is True and {lr.SD_CD_DEPOSIT, lr.SD_CD_65, lr.SD_CD_REFUND, lr.SD_MIRAMAR_RATES,
                                   lr.CALGREEN} <= ids(r)


@pytest.mark.parametrize("ptype,sqft,deposit,required", [
    ("nonresidential new construction", 60000, 10000.0, True),   # $0.20 x min(60,000, 50,000) = $10,000 (cap)
    ("nonresidential_new_construction", 1000, 200.0, True),      # $0.20 x 1,000 = $200 (not below $200)
    ("nonresidential new construction", 900, None, False),       # below the 1,000 sq ft minimum
    ("residential remodel", 1500, 1000.0, True),                 # flat $1,000 for 1,000-6,999 sq ft
    ("residential alteration", 6999, 1000.0, True),
    ("residential alteration", 7000, 2800.0, True),              # 7,000+ = non-res alteration: $0.40 x 7,000
    ("hotel remodel", 5000, 2000.0, True),                       # hotels = non-res alterations: $0.40 x 5,000
    ("demolition", 1000, 400.0, True),                           # bottom of the $400-$40,000 range
    ("commercial TI", 150000, 40000.0, True),                    # $0.40 x min(150,000, 100,000)
])
def test_sd_deposit_table(ptype, sqft, deposit, required):
    r = lr.construction_debris_plan("SD", ptype, sqft, as_of=AS_OF)
    assert r.deposit_required is required and r.deposit_usd == deposit
    if sqft > 100000 or (sqft > 50000 and "new" in ptype):
        assert any("inferred from the published deposit range" in n for n in r.notes)


@pytest.mark.parametrize("ptype,item", [("roofing", "roofing projects"), ("re-roof", "roofing projects"),
                                        ("fence", "fence"), ("electrical only", None), ("swimming pool", None),
                                        ("window replacement", "window")])
def test_sd_exempt_projects(ptype, item):
    r = lr.construction_debris_plan("SD", ptype, 3000, as_of=AS_OF)
    assert r.deposit_required is False and r.project_type.startswith("exempt: ")
    if item:
        assert r.exempt_reason == item
    assert {lr.SD_CD_EXEMPT, lr.CALGREEN} <= ids(r) and r.refund_outlook == "not_applicable"


def test_sd_full_refund_at_exactly_65_percent_legacy_rate_and_non_city_rate():
    full = lr.construction_debris_plan("SD", "demolition", 2000, total_debris_tons=20, diverted_tons=13,
                                       as_of=AS_OF)           # 13 / 20 = 65% -> full refund
    assert full.diversion_achieved_percent == 65.0 and full.refund_outlook == "full"
    assert full.additional_tons_needed == 0.0
    legacy = lr.construction_debris_plan("SD", "demolition", 2000, permit_issued_date="2016-06-30", as_of=AS_OF)
    assert legacy.diversion_required_percent == 50                         # permits issued through 2016-06-30
    out = lr.construction_debris_plan("SD", "demolition", 2000, total_debris_tons=10, diverted_tons=4,
                                      debris_generated_in_city=False, as_of=AS_OF)
    assert out.landfill_cost_usd == 1542.0                                 # 6 t x $257 (non-City waste)
    assert lr.construction_debris_plan("SD", "demolition", as_of=AS_OF).deposit_required is None


def test_la_debris_rules_and_self_haul():
    r = lr.construction_debris_plan("Los Angeles", "commercial TI", 5000, as_of=AS_OF)
    assert r.deposit_usd is None and r.verified is True
    assert {lr.LA_CD_PROC, lr.LA_CD_HAULER, lr.LA_PERMIT_HAULER} <= ids(r)
    s = lr.construction_debris_plan("LA", "commercial TI", 5000, hauling="self-haul", as_of=AS_OF)
    assert s.verified is False and s.unverified_rule_ids == [lr.VC23115_U]
    assert any("still needs the LASAN Private Waste Hauler Permit" in n for n in s.notes)


@pytest.mark.parametrize("value,deposit", [
    (250000, 7500.0),       # 3% x $250,000 = $7,500, between the $1,712 minimum and $56,999 cap
    (20000, 1712.0),        # 3% x $20,000 = $600 -> minimum $1,712
    (5000000, 56999.0),     # 3% x $5,000,000 = $150,000 -> cap $56,999
])
def test_long_beach_deposit_is_computed_but_unverified(value, deposit):
    r = lr.construction_debris_plan("Long Beach", "residential alteration", project_value_usd=value, as_of=AS_OF)
    assert r.deposit_usd == deposit and r.city_fee_usd == deposit and r.verified is False


@pytest.mark.parametrize("value,fee_usd", [(50000, 40.0),      # 0.04% x $50,000 = $20 -> $40 minimum
                                           (1000000, 400.0)])  # 0.04% x $1,000,000 = $400
def test_sacramento_admin_fee(value, fee_usd):
    r = lr.construction_debris_plan("Sacramento", "new home", project_value_usd=value, as_of=AS_OF)
    assert r.city_fee_usd == fee_usd and r.diversion_required_percent == 65 and r.verified is False


@pytest.mark.parametrize("kwargs,msg", [
    ({"project_type": "kitchen remodel"}, "is not clear"),
    ({"project_type": ""}, "project_type is required"),
    ({"project_type": "new home", "total_debris_tons": 10, "diverted_tons": 12}, "cannot exceed"),
    ({"project_type": "new home", "diverted_tons": 5}, "needs total_debris_tons"),
    ({"project_type": "new home", "square_feet": "big"}, "must be a number"),
    ({"project_type": "new home", "final_inspection_date": "soon"}, "ISO date"),
])
def test_debris_rejects_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        lr.construction_debris_plan("SD", **kwargs, as_of=AS_OF)


# ---------------------------------------------------------------- San Diego right-of-way fees
def test_sd_fees_ten_weeks_two_blocks_with_meters_dumpster_and_noise_permit():
    # IB 177: extension weeks = ceil(10 - 3) = 7; each request covers up to 6 weeks -> 2 requests.
    #   initial 2 x $131.52 = $263.04; extension $87.68 x 2 x 7 = $1,227.52; processing $87.68 x 3 = $263.04;
    #   meters $20 x 4 spaces x 10 days = $800; dumpster 3 weeks -> 1 x $176.57 + $87.68 processing;
    #   noise permit $529.71. Total = $3,347.56.
    r = lr.san_diego_right_of_way_fees(blocks=2, weeks=10, metered_spaces=4, meter_days=10, dumpster_weeks=3,
                                       noise_permit_applications=1)
    assert (r.extension_weeks, r.extension_requests, r.new_ds269_required) == (7, 2, False)
    assert [f.amount_usd for f in r.fees] == [263.04, 1227.52, 263.04, 800.0, 176.57, 87.68, 529.71]
    assert r.total_usd == 3347.56 and r.verified is True
    assert any("Meter covered" in n for n in r.notes) and any("IB 502" in n for n in r.notes)


def test_sd_fees_boundaries_and_options():
    three = lr.san_diego_right_of_way_fees(blocks=1, weeks=3)            # inside the 3-week initial period
    assert three.extension_weeks == 0 and three.total_usd == 219.20     # $131.52 + $87.68
    half = lr.san_diego_right_of_way_fees(blocks=1, weeks=3.5)           # ceil(0.5) = 1 extension week
    assert half.extension_weeks == 1 and half.total_usd == 394.56       # 131.52 + 87.68 + 87.68 x 2
    many = lr.san_diego_right_of_way_fees(blocks=1, weeks=10, extension_requests=4, modifications=1,
                                          modification_new_sheets=1)
    # 4 extension requests > 3 -> new DS-269; processing x (1 + 4 + 1) = $526.08; new sheet $131.52
    assert many.new_ds269_required is True and fee(many, "Traffic Control Processing").amount_usd == 526.08
    assert fee(many, "Modification new sheet").amount_usd == 131.52
    pod = lr.san_diego_right_of_way_fees(storage_container_days=10)      # $87.68 week + $87.68 processing
    assert pod.total_usd == 175.36 and any("up to 7 days" in n for n in pod.notes)
    posts = lr.san_diego_right_of_way_fees(meter_posts_removed=2, pay_stations_removed=1)
    assert posts.total_usd == 1200.0                                     # 2 x $300 + 1 x $600


@pytest.mark.parametrize("kwargs,msg", [
    ({}, "Nothing to price"),
    ({"blocks": 2}, "weeks is required"),
    ({"blocks": 2, "weeks": 10, "extension_requests": 1}, "at least 2 extension request"),
    ({"blocks": 2, "weeks": 4, "extension_requests": 2}, "cannot exceed"),
    ({"metered_spaces": 2}, "go together"),
    ({"modifications": 1}, "need blocks"),
    ({"blocks": "two", "weeks": 3}, "must be a number"),
    ({"blocks": 1.5, "weeks": 3}, "whole number"),
])
def test_fees_reject_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        lr.san_diego_right_of_way_fees(**kwargs)


# ---------------------------------------------------------------- MCP
def test_register_and_call_every_tool_through_an_mcp_client():
    mcp = FastMCP("t")
    lr.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            out = {
                "hours": await c.call_tool("construction_hours", {"city": " city of San Diego ",
                                                                  "when": "2026-11-26 10:00 am", "as_of": AS_OF}),
                "la": await c.call_tool("construction_hours", {"city": "L.A.", "when": "2026-11-28T08:30",
                                                               "work_type": "interior", "hcr_district": True,
                                                               "is_holiday": False, "as_of": AS_OF}),
                "job": await c.call_tool("local_jobsite_requirements", {
                    "city": "SD", "start_date": "2026-12-14T07:00", "work_in_public_right_of_way": True,
                    "blocks": 2, "weeks_in_street": 5, "as_of": AS_OF}),
                "debris": await c.call_tool("construction_debris_plan", {
                    "city": "San Diego", "project_type": "new home", "square_feet": 2400, "total_debris_tons": 40,
                    "diverted_tons": 22, "final_inspection_date": "2026-10-01", "as_of": AS_OF}),
                "fees": await c.call_tool("san_diego_right_of_way_fees", {"blocks": 2, "weeks": 10}),
            }
            return tools, {k: v.structured_content for k, v in out.items()}
    tools, out = asyncio.run(run())
    assert set(tools) == {"construction_hours", "local_jobsite_requirements", "construction_debris_plan",
                          "san_diego_right_of_way_fees"}
    for t in tools.values():
        a = t.annotations.model_dump()
        assert a.get("readOnlyHint", a.get("read_only_hint")) is True
        assert a.get("openWorldHint", a.get("open_world_hint")) is False
    # descriptions carry rulebook numbers
    assert "$529.71" in tools["construction_hours"].description and "9 p.m." in tools["construction_hours"].description
    assert "$131.52/block" in tools["san_diego_right_of_way_fees"].description
    assert "$0.40/sq ft" in tools["construction_debris_plan"].description
    assert out["hours"]["status"] == "needs_permit" and out["hours"]["holiday_name"] == "Thanksgiving Day"
    assert out["hours"]["verified"] is True and out["hours"]["citations"]
    assert out["hours"]["disclaimer"].startswith("Compliance information")
    assert out["la"]["status"] == "allowed" and out["la"]["allowed_windows"] == ["08:00-18:00"]
    # 2 blocks, 5 weeks: $263.04 + $350.72 + $175.36 = $789.12
    assert out["job"]["fees_total_usd"] == 789.12 and out["job"]["verified"] is True
    assert out["debris"]["deposit_usd"] == 960.0 and out["debris"]["refund_deadline"] == "2027-03-30"
    # 2 blocks, 10 weeks: $263.04 + $1,227.52 + $263.04 = $1,753.60
    assert out["fees"]["total_usd"] == 1753.6 and out["fees"]["extension_requests"] == 2


def test_mcp_rejects_bad_values_with_clear_errors():
    mcp = FastMCP("t")
    lr.register(mcp)

    async def run():
        async with Client(mcp) as c:
            errors = []
            for name, args in [
                ("construction_hours", {"city": "Irvine", "when": "2026-12-01T10:00"}),
                ("construction_hours", {"city": "SD", "when": "tomorrow morning"}),
                ("local_jobsite_requirements", {"city": "SD", "blocks": -1}),
                ("local_jobsite_requirements", {"city": "LA", "uses_street_for_bin": True, "bin_type": "crane"}),
                ("construction_debris_plan", {"city": "SD", "project_type": "kitchen remodel"}),
                ("san_diego_right_of_way_fees", {"blocks": 2}),
            ]:
                try:
                    await c.call_tool(name, args)
                    errors.append(None)
                except Exception as e:                         # fastmcp raises ToolError
                    errors.append(str(e))
            return errors
    errors = asyncio.run(run())
    assert all(errors), errors
    assert "No city jobsite rules for 'Irvine'" in errors[0] and "ISO date" in errors[1]
    assert "bin_type must be" in errors[3] and "is not clear" in errors[4] and "weeks is required" in errors[5]
