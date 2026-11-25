# California Construction Compliance Platform: Market Research

Research date: 2026-11-25. Not legal advice. Citations are tagged **[L#]** (law and regulations) and **[S#]** (market and pricing), and the full list is in section 7.

How the sources were checked: the CSLB *2026 License Law & Reference Book* is in this repo (`sources/2026_CSLB_Law_Book.pdf`, text in `sources/lawbook.txt`), and every B&P Chapter 9 quote below was read there directly. From this session, leginfo.legislature.ca.gov, dir.ca.gov and waterboards.ca.gov could not be fetched (DNS blocked by the session network). Other sections were confirmed through search excerpts of Justia, FindLaw, public.law, CSLB, DIR, CEC and State Water Board pages. A person must open each of those sections before the product relies on it. These items are marked *(verify)*. The CSLB counts come from the master-file snapshot in this repo (`data/cslb.sqlite`, built 2026-11-25). That snapshot is a partial download of 244,436 rows.

---

## 0. Executive summary

1. **The pitch has to change.** In California the platform cannot replace the licensed engineer, architect, attorney, insurance agent, QSD/QSP, HERS rater, competent person or CSLB qualifier. The law requires a named, licensed human for each of these. What it *can* replace is the **administrative and coordination work around them**, which is 40-100 hours a month in a 5-50 person contractor, or about 0.25-0.6 of a full-time employee. It can also replace **outside point services**: lien notice services, safety plan consultants, certified-payroll helpers, COI trackers and permit runners. It can catch the penalties those people miss.
2. **No competitor bundles California-specific compliance.** Procore and Autodesk are project management. Buildertrend is residential project management. ServiceTitan is field service management. Levelset/Procore Pay and Siteline cover payment and liens. LCPtracker covers public-works certified payroll. SafetyCulture is generic inspections. Highwire is enterprise prequalification. ADP and Paychex run payroll. None of them combines CSLB license, classification and workers' comp monitoring; Cal/OSHA written programs and permits; Labor Code wage-rule checks on field time; lien and prompt-payment deadlines; Title 24/HERS closeout; and DIR public-works duties, all in one place for a small firm.
3. **The data supports continuous license monitoring.** In the CSLB snapshot (2026-11-25), **11,833 licenses are suspended**: 6,403 for the bond, 3,270 for workers' comp, 722 for liability insurance and 241 for having no qualifier. 115,102 active licensees have a workers' comp policy on file, which marks them as employers and is a useful proxy for the serviceable market.
4. **Penalties are large enough to justify the price.** Cal/OSHA fines are up to $25,000 per serious violation and $162,851 per willful or repeat violation [L14]. Waiting-time penalties are up to 30 days of wages per employee [L17]. Hiring an unlicensed sub carries a $1,500-$30,000 CSLB civil penalty from July 1, 2026 (B&P 7099.2) [L1], plus Labor Code 2750.5 employer liability [L19]. Uninsured-employer penalties run $1,000-$10,000 per employee (Labor Code 3722) [L1]. Public works adds $100 per day per worker for late certified payroll records (LC 1776(h)) [L21].
5. **$1,000 a month is plausible for a GC or multi-trade firm with 15-50 people, and a stretch for a 5-10 person trade shop.** The firm's alternatives are an admin hire ($45-60 an hour loaded), a safety consultant (a vendor puts it at $12-18k a year), a lien service ($19-$49 per notice) and certified-payroll add-ons. Protekon, a California managed-compliance vendor, sells safety-only compliance at $597-$1,297 a month [S21], which sets a price anchor.
6. **MVP for the first 10 customers:** CSLB license and sub monitoring; sub prequalification packets; lien and prelim deadlines with statutory forms; Cal/OSHA program generator with toolbox-talk logs; a payroll-rule checker on imported time; permit fee and Title 24/HERS checklists. Every output carries citations and a "verified" status. Delivery is a web app, an MCP server and an offline field PWA. Most of the engine already exists in this repo (`tradecomply/`).

---

## 1. What software can take over, and what California reserves for a licensed human

### 1.1 Tasks software can legally and practically take over

Software can do these if it only calculates, tracks, checks or fills statutory or standard forms, and a responsible human reviews and signs where the law says so.

| Task area | Who does it today in a 5-50 person firm | What software can do | Legal guardrail and cite | Human who must stay in the loop |
|---|---|---|---|---|
| **Own CSLB license health** (renewal, bond, workers' comp, qualifier, classifications) | Owner or office manager, often reactive | Daily check against the CSLB master file; alerts before expiration or workers' comp lapse. A workers' comp lapse suspends the license automatically (B&P 7125.2), and any work done while unlicensed forfeits pay under B&P 7031. | B&P 7125, 7125.2, 7031 [L1][L20] | Qualifier (RMO/RME) stays responsible (B&P 7068.1) [L1] |
| **Sub license and classification check, prequalification** | PM or office manager calls or looks up CSLB | Bulk check: status, classes against scope, bond, workers' comp. Flag the classes that need workers' comp even with no employees (C-8, C-20, C-22, C-39, D-49, under B&P 7125(b)(2); universal workers' comp starts Jan 1, 2028, per SB 1455). Flag unlicensed or suspended subs before contract signing. | B&P 7125, 7118, 7099.2; LC 2750.5 [L1][L19][L33] | PM decides; nobody may contract with an unlicensed sub |
| **Certificates of insurance (COI) and endorsements** | Office manager chases PDFs | OCR the COI, compare it with the contract requirements the customer entered, track expirations, send requests | Comparing a COI with stated contract limits is not "transacting insurance." Recommending coverage is, and needs a license (Ins. Code 1631) [L8] | Licensed broker advises on coverage |
| **Lien and notice management** | Office manager or an outside lien service | Calendar for the 20-day preliminary notice, the 90/60/30-day lien recording windows and the 90-day foreclosure deadline. Fill the statutory waiver forms, track proof of service, calculate 2%/month prompt-payment interest (B&P 7108.5). | Civ. Code 8204, 8412, 8414, 8460, 8132-8138 *(verify)*; B&P 7108.5 (read in law book) [L1][L22][L23] | Attorney for lien foreclosure suits and disputes |
| **Home improvement contract compliance** | Owner, using templates | Check required contents and the down-payment cap (no more than $1,000 or 10%, whichever is less) | B&P 7159, 7159.5(a)(3) [L1] | Owner signs; attorney for custom terms |
| **Permits and fee estimates** | PM, permit runner or expediter | Published fee tables (San Diego IB-501 is already built), state SMIP and building-standards fees, document checklists, Cal/OSHA activity permits (8 CCR 341: $50 project, $100 annual, 24-hour notice) | City schedules; 8 CCR 341 [L11] | Building official decides; the applicant signs |
| **Title 24 / HERS closeout (HVAC)** | Tech, office and HERS rater coordination | Detect triggers (refrigerant charge, duct leakage, airflow in alterations); track CF1R, CF2R and CF3R and permit closure. Utility rebates require proof of permit closure (SB 1414). | 2025 Energy Code (in effect Jan 1, 2026); Title 20 §1670-1675 [L12][L13] | HERS rater (independent, Title 20 §1673) |
| **Safety program documents** | Owner, a consultant, or nobody | Draft the IIPP (8 CCR 1509 and 3203), Code of Safe Practices, Heat Illness Prevention Plan (8 CCR 3395), Workplace Violence Prevention Plan (LC 6401.9). Schedule toolbox talks at least every 10 working days (8 CCR 1509(e)) *(verify)*. Keep training records and remind about the 8-hour serious-injury report (8 CCR 342) *(verify)* and the Form 300/300A log. | 8 CCR 1509, 3203, 3395, 341; LC 6401.9 [L10][L11][L16] | IIPP "responsible person" named by the employer; competent and qualified persons on site |
| **Payroll rule checks** (not running payroll) | Payroll clerk plus ADP or Paychex | Pre-payroll checks on field time: daily and 7th-day overtime, meal and rest premiums, minimum wage ($16.90 state in 2026, higher in some cities), exempt salary test ($70,304), final pay on layoff day (LC 201/203), wage statement fields (LC 226), prevailing-wage rate by craft and county | LC 201-203, 226, 510, 512, 1770 et seq. [L17][L24][L25] | Employer remains liable; a human certifies certified payroll records under penalty of perjury (LC 1776) |
| **Public works administration** | Payroll or office staff | Check DIR registration (LC 1725.5), eCPR uploads (LC 1771.4), deadlines for requested payroll records, apprenticeship forms | LC 1725.5, 1771.4, 1775, 1776, 1777.5, 1813 *(verify)* [L21] | Signer of the certified payroll records |
| **HR compliance** | Owner or bookkeeper | Track harassment-prevention training for employers with 5+ employees (1 hour for staff, 2 hours for supervisors, every 2 years, interactive) under Gov. Code 12950.1; new-hire notices; sick leave balances | Gov. Code 12950.1 [L26] | HR or attorney for investigations, terminations and accommodations |
| **Engineering-adjacent calculations** | Contractor, or a paid PE | HVAC/plumbing load and sizing calcs for the contractor's own installs; pre-check whether a structure falls inside the conventional wood-frame exemptions; prepare an input packet for the PE | B&P 6737.3 (contractor may design the MEP systems it installs); B&P 5537 and 6737.1 (exemptions) [L3][L4][L5] | PE or architect when the work falls outside the exemptions, or when the building official requires one |

### 1.2 What California reserves for a licensed or designated person

| Function | Rule | What software cannot do | What software can do |
|---|---|---|---|
| **Civil and structural engineering documents** | B&P 6735: civil engineering (including structural and geotechnical) plans, calculations, specifications and reports must be prepared by or under the responsible charge of a licensed civil engineer. Documents released for construction must be signed and sealed with the date, on every plan sheet. Interim documents must be marked "preliminary" or "not for construction" [L2]. | Sign or stamp anything, or act as the engineer in responsible charge | Draft calcs for PE review, label them "NOT FOR CONSTRUCTION", screen for when a PE is needed |
| **Engineering exemptions** | B&P 6737.1: unlicensed people may prepare plans for conventional wood-frame dwellings (up to 2 stories plus basement, no more than 4 units on a lot), garages and agricultural buildings. **Any portion that deviates from conventional framing must be stamped by a licensed engineer or architect** [L3]. B&P 6737.3: licensed contractors are exempt for **electrical and mechanical** design of systems they install and supervise, and may prepare shop drawings; this does not authorize design for installation by others [L4]. | Decide definitively that the exemption applies (the building official can always require a licensed design professional) | Run an exemption screen ("likely exempt / likely needs PE"), with the building official having the final word |
| **Architecture** | B&P 5536: unlicensed practice or stamp misuse is a misdemeanor; CAB citations of $250-$6,000 are typical [L6]. B&P 5537 exempts wood-frame single-family homes up to 2 stories plus basement, multifamily up to 4 units, garages, and agricultural and ranch buildings [L5]. Nonstructural storefronts and interior alterations are also exempt *(verify subdivision)*. B&P 5537.2 covers contractor exemptions *(verify)*. | Design non-exempt buildings | Same screening and packet preparation as above |
| **Deep excavations** | 8 CCR 1541.1: excavations over 20 ft, or protective systems that depart from the tabulated options, need a design by a registered professional engineer. Tabulated data naming the PE must be kept on site [L10]. | Approve a protective system | Track the PE design document and the daily competent-person inspection |
| **Competent person** (excavations, silica, and others) | 8 CCR 1504 definition; 1541(k): the competent person inspects daily and as conditions change, and removes workers when a hazard is found [L10] | Be on site | Hold the named competent person per job, the daily inspection checklist and the record |
| **SWPPP (1 acre or more of disturbance)** | Construction General Permit, Order 2022-0057-DWQ: the SWPPP must be developed by a **QSD** and implemented by a **QSP**. PEs and PGs can self-certify eligibility; others need an approved prerequisite plus CASQA training of record [L9]. | Write and sign the SWPPP, or perform QSD/QSP inspections | Calendar rain-event and inspection duties, collect photos and logs, remind about SMARTS filings |
| **HERS field verification** | Title 20 §1673(i)-(j): the rater must be **independent** of the builder or installer. An employment relationship counts as a disqualifying financial interest [L12]. | Run the test, or let the installer self-verify | Detect triggers, schedule the rater, track CF3R registration and permit final |
| **Legal advice** | B&P 6125: only active State Bar licensees may practice law [L7]. B&P 6400-6415: people paid to provide "self-help" legal document services must register as a Legal Document Assistant; 6411 bars UPL [L27]. | Give tailored legal advice, draft custom contract terms, or represent anyone in lien foreclosure | Deadline arithmetic, statutory forms filled with the user's facts (as Levelset does), plain-language summaries with "consult an attorney" triggers. **Get a written opinion from a California attorney on the LDA question before charging for document preparation.** |
| **Insurance** | Ins. Code 1631: a license is required to solicit, negotiate or effect insurance [L8]. | Recommend coverages or carriers, or bind coverage | Read the COI and compare it with requirements the customer states |
| **CSLB qualifier** | B&P 7068/7068.1: the license qualifier (RMO/RME) must exercise supervision and control. An RME must be "actively engaged" 32 hours a week or 80% of business hours [L1]. | Be the qualifier | Monitor the qualifier record (241 licenses are suspended today for "No Qualifier") |
| **Trade certifications** | LC 108: electricians working for C-10 contractors must be DIR-certified [L35]. EPA Section 608 certification for refrigerant work (federal). Crane operator certification (8 CCR 5006.1) *(verify)*. | Hold the certification | Track certification and expiration per worker |
| **Special inspections, land surveying** | California Building Code Chapter 17 special inspectors; B&P 8726 licensed land surveyor for boundaries and staking *(both verify)* | Perform them | Schedule them and track the reports |

### 1.3 The honest answer to "replace engineers, managers, HR and admin"

| Role the founder wants to replace | Realistic outcome |
|---|---|
| Office manager / compliance admin | **Mostly replaceable** (about 50-70% of compliance hours): license, COI, notice, permit and training tracking |
| Payroll clerk | **Partly**: pre-payroll rule checks and certified-payroll preparation. The payroll provider still runs payroll, and a human signs the certified payroll records. |
| HR generalist | **Partly**: policies, notices, training tracking. Investigations, discipline, terminations and accommodations still need a human or counsel. |
| Safety manager | **Partly**: written programs, schedules, logs, reminders. The competent person, inspections and incident investigation stay human. Free Cal/OSHA Consultation can review programs [L31]. |
| Project manager / project engineer | **Assist only**: deadlines, document control, permit packets. Judgment stays human. |
| Licensed engineer or architect | **Not replaceable** for non-exempt work. Software reduces engineer hours by preparing inputs and screening out work that does not need a stamp. |
| Attorney, insurance broker, QSD/QSP, HERS rater | **Not replaceable**. Route the customer to them through partners or a marketplace. |

The positioning that holds up legally: *"Your compliance back office for California: it does the tracking, paperwork and checks a coordinator, a lien service and a safety consultant do today, and tells you exactly when you need a licensed professional."*

---

## 2. Competitors and pricing

| Product | Segment | Pricing (2026, best available) | What it does not cover for California compliance |
|---|---|---|---|
| **Procore** | Mid and large GCs, owners | Custom, by annual construction volume (ACV), unlimited users. Third-party estimates for small GCs run from about $4-6k to $15-30k a year, with 10-20% increases reported at renewal [S1][S2][S3]. Helix AI and Agent Builder in open beta since Oct 2025 [S22]. Third-party MCP connectors exist (Composio) [S22]. | No CSLB master-file monitoring or classification-to-scope checks, no Cal/OSHA written-program generator, no California wage-rule checks. Lien rights are sold separately through Levelset. Expensive for 5-50 person firms. |
| **Autodesk Construction Cloud / Forma** (Autodesk Build) | Mid and large GCs, design-build | About $135-165 per user per month; $1,625 per user per year list [S7][S8]. Acquired Rhumbix (field time, T&M, offline) in 2025 [S11]. Autodesk MCP servers: Product Help (Apr 2026), Revit public MCP (Jun 2026), Fusion Data [S23]. | Same gaps as Procore. Per-seat pricing penalizes office staff. |
| **Buildertrend** | Residential builders and remodelers | No public price. Last published (late 2024): Essential $399, Advanced $599, Complete $829 per month billed annually; 2026 estimates $339-$1,099 per month [S4][S5][S6]. | Generic contract templates (B&P 7159/7159.5 content and down-payment cap are not enforced), no lien-deadline engine for California, no CSLB or Cal/OSHA functions |
| **CoConstruct** | Custom home builders | Being retired into Buildertrend: no new projects after about Mar 31 / Apr 1, 2027; migrations must start by Jun 30, 2027 [S15] | Same as Buildertrend. **Displaced users are a 2027 sales opportunity.** |
| **Levelset (Procore)** | Subcontractors and suppliers | Per document: notices about $19-29, intent-to-lien about $49, lien about $349; subscriptions about $149 per user per month per review sites (sources conflict) [S9] | Lien and payment only. No license, workers' comp, safety, payroll or permit functions. |
| **Siteline** | Trade contractors (pay applications) | Custom annual price based on billing volume, unlimited users; lien rights management is an add-on [S10] | Billing and waivers only |
| **Rhumbix** (now Autodesk) | Self-performing trades | Quote-based | Field time and production. No California rule engine (daily overtime, meal premiums) is documented. |
| **SafetyCulture** | Any industry | $24 per seat per month annual ($29 monthly); free up to 10 users [S12] | General-purpose. Title 8 programs (IIPP, heat, WVPP), §341 permits and toolbox cadence depend on templates the user builds. |
| **Highwire** | GCs and owners (enterprise prequal) | G2 (Jan 2025): $1,299-$4,199 a month; subs often enroll free [S13] | Built for large GCs and owners, not 5-50 person firms. Not a CSLB-native classification check. |
| **Billd** | Commercial subs with more than $2M revenue | Material financing; repay within 120 days; rates not published [S14] | Not compliance (a possible partner) |
| **ServiceTitan** | HVAC, plumbing and electrical service | About $245-$500 per technician per month plus $5k-$50k implementation (third-party) [S16] | We found no documented Title 24/HERS closeout tracking, CSLB sub monitoring or Cal/OSHA programs. Customers already pay $2-4k a month, so position as an add-on or integration. |
| **ADP / Paychex** | Payroll | ADP custom (about $15-30 per employee per month reported); Paychex from $39 plus $5 per employee; certified payroll through add-ons or partners (LCPtracker with Paychex) [S18][S17] | Run payroll from hours entered. They do not check field reality (for example a layoff at job end needing final pay that day, or missed meal premiums), and do not cover safety, liens or licenses. |
| **LCPtracker (LCPcertified)** | Public-works contractors | Free first project; paid plans about $150 a month and up (third-party); supports DIR XML upload [S17] | Public works only. No private-works LC 218.7 documentation, safety, liens or CSLB. |
| **Others** | | JobTread $199 a month plus $20 per user [S20]; Contractor Foreman from $49 per user per month [S20]; Fieldwire free basic plan [S20]; certified-payroll tools $79-$400 a month [S18]; COI tracking $3-$10 per vendor per year self-service, $10-$30 managed [S19]; **Protekon** (California managed compliance: IIPP, WVPP, CSLB monitoring) $597-$1,297 a month (vendor claims) [S21] | Each covers one slice. Protekon is the closest California-specific competitor at a similar price point, but it is safety and HR, without liens, payroll, permits or public works. |

**California gaps nobody bundles for small firms** (these are the product wedge):
1. **Monitoring from the CSLB master file**: status, classification scope, bond, workers' comp, qualifier, and the classes that need workers' comp regardless (SB 216/SB 1455), for the firm's own license and every sub.
2. **Unlicensed-sub exposure**: B&P 7118 discipline with 7099.2 penalties, LC 2750.5 deemed-employee liability, and LC 218.7 private-works wage liability, with documentation requests to subs.
3. **Title 8 construction programs**: IIPP §1509, Code of Safe Practices, heat §3395, WVPP 6401.9 (or documenting the under-10-employee, non-public exemption), §341 permits, toolbox cadence.
4. **California wage rules applied to field time**, before payroll runs.
5. **Lien, prompt-payment and retention deadlines tied to the same project record**, with statutory waiver forms.
6. **Title 24/HERS closeout and permit-fee estimates by jurisdiction** for HVAC and plumbing.
7. **DIR public-works stack** (registration, eCPR, penalties) for firms with only occasional public work.

---

## 3. What makes a 5-50 person California contractor pay $1,000 a month

### 3.1 Penalties and losses avoided (maximum exposure per event)

| Risk | Exposure | Cite |
|---|---|---|
| Cal/OSHA citation | Serious: up to $25,000 per violation. General/regulatory: up to $16,285. Willful/repeat: up to $162,851 (willful minimum $11,632). Construction IIPP §1509 was #3 on the 2024 most-cited list. | 8 CCR 336; DIR 2025-10 [L14][L15] |
| Waiting-time penalty | Up to 30 days of daily wages per separated employee. At $45/h × 8 h × 30 = **$10,800 per worker**. Construction layoffs at job end count as discharges (pay is due immediately, LC 201). | LC 201-203 [L17] |
| Wage statement and PAGA | PAGA after the 2024 reform: $100 per employee per pay period (technical wage-statement errors capped at $25), with cure options | LC 226; PAGA 2024 reform [L24] |
| Own license lapses (workers' comp or bond) | Automatic suspension; work done while unlicensed means no right to sue for pay **and** disgorgement of all compensation received (no offset for materials or labor) | B&P 7125.2, 7031; *White v. Cridlebaugh*; *MW Erectors* [L1][L20] |
| Hiring an unlicensed sub | CSLB civil penalty of **$1,500-$30,000** (7099.2, operative July 1, 2026). The sub's workers are deemed the hirer's employees (workers' comp and wage claims). Wage liability on private works (LC 218.7). | [L1][L18][L19] |
| Uninsured employer | Greater of 2× premium or $1,000 per employee; $2,000 or $10,000 per employee once a claim is filed; stop order; misdemeanor | LC 3700.5, 3710.1, 3722 [L1] |
| Unlicensed contracting (for the firm itself) | Citation $1,500-$15,000 (7028.7, operative July 1, 2026); misdemeanor up to $5,000 or 6 months, and 20% of the contract price for repeat offenses (7028) | [L1] |
| Lost lien rights | A late preliminary notice only reaches back 20 days. Missing the 90/60/30-day recording window loses the lien entirely. Average sub wait is about 51 days after a pay application; 43% of subs wait more than 90 days for retention. | Civ. Code 8204, 8412, 8414 *(verify)*; Billd 2026; Siteline 2026 [L22][S24][S25] |
| Late payment to or from subs | 2% per month penalty plus attorney fees (B&P 7108.5), which a sub can claim | [L1][L32] |
| Public works | 1776(h): $100 per day per worker for records not produced within 10 days. 1771.4: $100 per day, up to $5,000 per project, for eCPR. 1775: up to $200 per day per worker underpaid. 1813: $25 per day per worker for overtime. | [L21] *(verify amounts)* |
| Home improvement contracts | A down payment above $1,000 or 10% is cause for discipline | B&P 7159.5 [L1] |
| Workers' comp cost | Statewide advisory pure premium $1.65 per $100 of payroll (+6.6%, Sept 1, 2026). Construction classes are much higher. A better safety record lowers the experience mod. | CDI release 024-2026 [L28] |

### 3.2 Staff time and outside spend replaced

The hours in this section are assumptions to validate in discovery interviews. Wage anchors: BLS mean for payroll/timekeeping clerks in San Diego is $67,300 (May 2024); California construction managers average $129,690 (May 2023); coordinator salaries range from $50.7k (Payscale) to $125k (Salary.com) [L30]. We use **$45-$60 an hour loaded** for compliance admin time.

**Persona A: 30-person commercial GC** (B license), 8 active projects, 25 active subs, 1-2 public-works jobs.

| Work | Hours per month today | Automatable | Value per month |
|---|---|---|---|
| Sub license, workers' comp, COI and prequal checks | 10-15 | 70% | $350-$630 |
| Prelim notices, lien calendar, waiver collection per pay application | 15-25 | 60% | $400-$900 |
| Certified payroll preparation and sub payroll review (public works) | 12-20 | 50% | $270-$600 |
| Safety: site plans, toolbox logs, sub safety documents, §341 permits | 15-20 | 50% | $340-$600 |
| LC 218.7 sub wage-document requests, HR notices and training | 5-8 | 60% | $135-$290 |
| **Total** | **57-88 h (0.35-0.55 FTE)** | | **about $1,500-$3,000 a month in time**, plus avoided services: a managed COI service at $10-$30 per vendor per year, and the lien-notice service a sub would otherwise pay $19-$49 per notice for. A GC mostly receives prelims and collects waivers rather than sending notices. Penalty risk also drops. |

**Persona B: 15-person HVAC/plumbing shop** (C-20/C-36), residential changeouts plus light commercial, about 25 permitted jobs a month.

| Work | Hours per month | Value per month |
|---|---|---|
| Permit packet, fee estimate, Title 24 CF1R/CF2R, HERS scheduling and closeout (about 30-45 minutes saved per job) | 12-19 | $540-$1,140 |
| Own license, bond and workers' comp (C-20 must carry workers' comp); EPA 608 and electrician certification tracking | 2-3 | $90-$180 |
| Payroll rule checks (overtime, meal premiums, final pay) | 4-6 | $180-$360 |
| Safety and HR programs, toolbox talks, heat plan | 4-6, or a $600-$1,300 a month managed service | $180-$1,300 |
| **Total** | **22-34 h** | **about $1,000-$2,900 a month**. **$1,000 sits at the top of what this persona will pay, so offer a $499-$699 trade tier or a ServiceTitan add-on.** |

**Persona C: small developer or owner-builder.** Value comes from prequalifying GCs and subs, collecting conditional and unconditional waivers before each draw (lender requirement), recording the notice of completion within 15 days (Civ. Code 8182), SWPPP and QSD vendor tracking, and the special-inspection calendar. They pay per project ($300-$800 a month during construction) rather than year-round.

### 3.3 Features that close the sale, in priority order

1. A **"license health" scan in 60 seconds** on the firm's own license and its sub list. The snapshot shows about 4.8% of licenses suspended, so most firms with 20+ subs will see at least one red flag.
2. A **lien deadline calendar** and statutory waivers per project, plus prompt-payment interest the firm is owed.
3. **Cal/OSHA programs generated from a 10-minute questionnaire**, plus a toolbox-talk log the field can sign offline.
4. **Payroll pre-check** on an imported time CSV that shows "penalties you would have owed this month".
5. **Permit and HERS closeout** for HVAC, and **public-works readiness** for GCs.

---

## 4. Packaging, onboarding and MVP

### 4.1 Product shape

- **Web app** (office): dashboard of red/amber/green risks per company, project and sub; a document vault; a calendar; an audit log. Every rule result shows its citation, the date the rule was checked and a `verified` flag, as `tradecomply/rules.py` already does.
- **MCP server** (AI agents): the existing 5 tools (`verify_contractor_license`, `estimate_permit_fees`, `lookup_safety_standard`, `calculate_lien_deadline`, `check_subcontractor_compliance`), plus `generate_safety_program`, `check_payroll_rules`, `public_works_readiness`, `title24_hers_triggers`, `list_deadlines` and `draft_statutory_waiver`. Read-only by default. Write or "send" actions such as mailing a notice require approval in the app. Every call is logged per tenant. This lets customers' Claude/ChatGPT/Copilot agents, and Procore Helix or Autodesk agents, use the California rule engine; both vendors are moving to MCP [S22][S23].
- **Offline and local mode:**
  - A field PWA that works offline for toolbox talks, signatures, daily inspections (excavation competent person, scaffold), photos and incident first reports, syncing later. Rhumbix's offline support shows field crews expect this [S11].
  - A local or desktop mode that ships the CSLB snapshot and rules pack (already bundled in `seed/`) and labels every result with the snapshot date. This suits privacy-sensitive payroll data and job sites with no connection.
- **Integrations, not rebuilds:** QuickBooks/Sage vendor and payroll exports, ADP/Paychex/Gusto CSV, Procore and Buildertrend project import, a certified-mail API for notices, DIR eCPR XML export.
- **Pricing:**
  - **Trade**: $499-$699 a month, up to 15 employees, 5 active projects.
  - **Core**: $1,000 a month, up to 50 employees, 15 projects, 50 monitored subs, unlimited office users.
  - **Public Works add-on**: +$300-$500 a month.
  - Pass-through usage costs: certified mail and recording fees.
  - Annual plan with 2 months free. **Founding-customer price** of $750 a month for 12 months in exchange for a case study and a weekly feedback call.

### 4.2 Onboarding flow (value in 15 minutes, full setup in 1 week)

1. **Enter the CSLB license number.** Pull classes, bond, workers' comp, qualifier and expiration. Show the license health card and alerts.
2. **Upload the sub list** (CSV or QuickBooks vendors). Run the bulk CSLB check, the class-to-scope match and the workers' comp-required-class check. Show the risk report (the "aha" moment).
3. **Add active projects**: address, owner, lender, contract value, first-furnishing date, public or private. Generate the lien calendar, draft prelim notices and set public-works flags.
4. **Company questionnaire** (headcount at each site, public access, heat exposure, excavation, scaffolds, silica). Generate draft IIPP, Code of Safe Practices, heat plan, and WVPP or a documented exemption. Set the toolbox cadence.
5. **Payroll import** (last 2 pay periods). Show a rule-check report with estimated penalties avoided.
6. **Invite field staff** to the PWA, set SMS and email alerts, and connect AI agents over MCP.
7. **Day-7 review**: a California compliance specialist (later a partner attorney or safety professional) reviews the generated documents. The customer signs and names the responsible persons. Items are marked verified.

### 4.3 MVP that must exist before selling to the first 10 customers

| # | Feature | Status in repo |
|---|---|---|
| 1 | CSLB own-license and sub monitoring with daily refresh, suspension reasons, workers' comp-required classes and alerts | Built (master-file import, 244k rows); add alerts and multi-tenant support |
| 2 | Sub prequal packet: license, COI expirations, W-9, DIR number, signed safety acknowledgment | Partial (`check_subcontractor_compliance`) |
| 3 | Lien engine: 8204/8412/8414/8460 calendar, notice of completion effects, statutory waivers 8132-8138, 7108.5 interest | Deadlines built; forms and interest still to build |
| 4 | Cal/OSHA programs plus toolbox log plus §341 permit check plus 8-hour reporting reminder | Safety lookup built; generator to build |
| 5 | Payroll rule checker on imported time | To build |
| 6 | Permit fees (San Diego done; add LA, San Diego County, San Jose, Sacramento only from verified tables) and a Title 24/HERS trigger checklist | San Diego built |
| 7 | Audit trail, citations, `verified` workflow, disclaimers, per-tenant data isolation | Citations and flags built |
| 8 | MCP over HTTP plus web UI plus offline PWA for the field | MCP built; UI and PWA to build |

**Out of scope for the MVP:** RFIs and submittals, scheduling, accounting, running payroll, design calcs, and any "legal advice" chat.

**First-10 go-to-market:**
- Start in San Diego, where permit data is already verified: HVAC/plumbing shops with 10-40 people and small commercial GCs with occasional public work.
- Channels: ACCA, PHCC, AGC San Diego, ABC SoCal, workers' comp brokers and surety agents (whose clients need safety programs and license hygiene), and construction CPAs.
- Use the CSLB master file for targeting (filter: active, workers' comp on file, classes B/C-20/C-36/C-10). Follow CAN-SPAM and the CSLB data terms.

### 4.4 Risks

- **Liability for a wrong deadline**, which can mean lost lien rights. Mitigate with citations, conservative (earliest) dates, human confirmation, technology E&O insurance and limits in the terms of service.
- **Unauthorized practice of law and LDA status.** Limit document work to statutory forms and arithmetic, and get a California attorney opinion on B&P 6400 before charging for document preparation.
- **Rule drift.** California changes on Jan 1 and July 1 each year (for example, the SB 779 penalties took effect July 1, 2026, and universal workers' comp starts Jan 1, 2028). Budget for a recurring legal content review.
- **Bundling by incumbents.** Procore, Autodesk and ServiceTitan could add California packs. The moat is depth of California rules, CSLB data plumbing, and the price and onboarding for 5-50 person firms.
- **Churn once documents are generated.** Keep value continuous through monitoring, alerts, deadlines and payroll checks.

---

## 5. Data gathered in this session

CSLB snapshot (`data/cslb.sqlite`, built 2026-11-25; partial file of 244,436 rows):
- Status: active 232,535; suspended 11,833; inactive 66.
- Active licenses by workers' comp status: on file 115,102 (employers); exempt 113,475; none 3,582.
- Top suspension reasons: bond 6,403; workers' comp 3,270; liability insurance 722; workers' comp suspension pending 249; no qualifier 241; family support 240.
- Active licenses by class: B 103,042; C-10 27,990; C-36 17,535; C-33 15,422; A 14,633; C-27 12,020; C-20 11,608.
- Active licenses with workers' comp on file: B 44,278; C-20 11,599; C-10 13,895; C-36 8,595.
- CSLB's own study counts 242,847 active licensees (Dec 2025); the CSLB homepage says about 285,000 [L29].

---

## 6. Open items to verify before launch

1. Open on leginfo and confirm: Civ. Code 8204, 8412, 8414, 8460, 8182, 8132-8138; LC 203, 226, 1725.5, 1771.4, 1775, 1776, 1813, 218.7, 2750.5, 6401.9; B&P 5537, 5537.2, 6735, 6737.1, 6737.3, 6125, 6400; Ins. Code 1631.
2. Confirm on dir.ca.gov: 8 CCR 1509(e) (10 working days), 1541, 1541.1, 3203, 3395, 341, 342.
3. Confirm on waterboards.ca.gov: CGP QSD/QSP duties by risk level, and the approved prerequisite list.
4. Confirm on energy.ca.gov: 2025 Energy Code §150.2(b) HVAC alteration triggers.
5. Get direct vendor quotes for Procore, Buildertrend, Siteline, Highwire, ServiceTitan and LCPtracker; every price above is third-party or vendor marketing.
6. Run 15-20 discovery interviews to replace the hour estimates in section 3.2 with measured numbers.

---

## 7. Sources

**Law and regulations**
- [L1] CSLB, *2026 California Contractors License Law & Reference Book* (read directly: B&P 7028, 7028.7, 7031, 7068, 7068.1, 7099.2, 7108.5, 7118, 7125, 7159.5; LC 3700.5, 3710.1, 3722 summaries). Local copy: `sources/2026_CSLB_Law_Book.pdf`; online: https://www.cslb.ca.gov/Resources/IndustryBulletins/2026/Law%20Book.FINAL.pdf
- [L2] B&P 6735: https://law.justia.com/codes/california/code-bpc/division-3/chapter-7/article-3/section-6735/ ; DSA IR A-19 (stamping): https://www.dgs.ca.gov/-/media/Divisions/DSA/Publications/interpretations_of_regs/IR_A-19.pdf
- [L3] B&P 6737.1: https://california.public.law/codes/ca_bus_and_prof_code_section_6737.1
- [L4] B&P 6737.3: https://law.justia.com/codes/california/code-bpc/division-3/chapter-7/article-3/section-6737-3/ ; https://law.counselstack.com/statute/ca/BPC/6737.3.
- [L5] B&P 5537: https://law.justia.com/codes/california/code-bpc/division-3/chapter-3/article-3/section-5537 ; CAB design limitations: https://cab.ca.gov/docs/misc/design_limitations.pdf ; Sacramento "Who May Prepare Plans": https://www.cityofsacramento.gov/content/dam/portal/cdd/Building/Forms/CDD-0236_Who-May-Prepare-Plans.pdf
- [L6] B&P 5536 / 5536.1: https://law.justia.com/codes/california/code-bpc/division-3/chapter-3/article-3/section-5536/ ; CAB Practice Act: https://cab.ca.gov/act/architects-practice-act-2023.pdf ; CAB enforcement: https://www.cab.ca.gov/enf/enf_actions/d.shtml
- [L7] B&P 6125: https://law.justia.com/codes/california/code-bpc/division-3/chapter-4/article-7/section-6125
- [L8] Ins. Code 1631: https://california.public.law/codes/ca_ins_code_section_1631
- [L9] Construction General Permit 2022-0057-DWQ: https://www.waterboards.ca.gov/water_issues/programs/stormwater/construction/docs/2022-0057-dwq-with-attachments/cgp2022_order.pdf ; Attachment D: https://waterboards.ca.gov/water_issues/programs/stormwater/construction/docs/2022-0057-dwq-with-attachments/cgp2022_att_d.pdf ; QSD/QSP prerequisites: https://Waterboards.ca.gov/water_issues/programs/stormwater/construction/docs/2024/2022-cgp-qsdqsp-prereq.pdf ; CASQA: https://www.casqa.org/training/cgp-training/qsd-qsp-overview
- [L10] 8 CCR 1541: https://dir.ca.gov/title8/1541.html ; 1541.1: https://dir.ca.gov/Title8/1541_1.html ; Caltrans Trenching & Shoring Manual ch. 2: https://dot.ca.gov/-/media/dot-media/programs/engineering/documents/structureconstruction/ts/ts-chpt-2-a11y.pdf
- [L11] 8 CCR 341: https://dir.ca.gov/Title8/341.html ; Cal/OSHA permit fact sheet: https://dir.ca.gov/dosh/dosh_publications/Construction-Activity-Permits-Employers.pdf ; FAQ: https://www.dir.ca.gov/dosh/enforcement-faq.html
- [L12] CEC HVAC FAQ (2025 code): https://www.energy.ca.gov/programs-and-topics/programs/building-energy-efficiency-standards/energy-code-support-center/hvac-0 ; RA2 field verification table: https://energycodeace.com/content/cy25-table-ra2-1-summary-of-measures-requiring-field-verification-and-diagnostic-testing ; 2025 residential forms: https://energycodeace.com/ResidentialForms/2025 ; Title 20 §1673: https://www.law.cornell.edu/regulations/california/20-CCR-1673 ; independence glossary: https://energycodeace.com/site/custom/public/reference-ace-2019/Documents/gloss_independentidentity.htm
- [L13] HVAC permit compliance (SB 1414, about 90% unpermitted estimate): https://energycenter.org/program/hvac-permit-compliance-financing-pilots ; https://www.nrdc.org/bio/kiki-velez/poor-quality-hvac-installs-are-costing-us-solution-within-reach
- [L14] DIR news 2025-10 (Cal/OSHA penalty adjustment): https://dir.ca.gov/DIRNews/2025/2025-10.html ; 8 CCR 336: https://www.dir.ca.gov/Title8/336.html ; federal OSHA 2026 (no adjustment): https://www.osha.gov/memos/2026-05-21/2026-annual-adjustments-osha-civil-penalties
- [L15] Cal/OSHA most frequently cited standards: https://dir.ca.gov/dosh/statistics/Frequently-cited-standards.html ; construction guide: https://www.dir.ca.gov/dosh/construction-guide-summary.html
- [L16] SB 553 / LC 6401.9: https://dir.ca.gov/dosh/Workplace-Violence/Laws-in-General-Industry.html ; https://law.justia.com/codes/california/code-lab/division-5/part-1/chapter-3/section-6401-9 ; exemptions: https://natlawreview.com/article/californias-workplace-violence-prevention-law-turns-two-part-3-key-exemptions-and
- [L17] LC 203: https://law.justia.com/codes/california/code-lab/division-2/part-1/chapter-1/article-1/section-203/ ; CACI 2704: https://justia.com/trials-litigation/docs/caci/2700/2704
- [L18] LC 218.7: https://codes.findlaw.com/ca/labor-code/lab-sect-218-7/ ; https://www.swlaw.com/publications/legal-alerts/contractors-on-private-works-now-liable-for-unpaid-wages-owed-by-its-subcontractors
- [L19] LC 2750.5: *Neighbours v. Buzz Oates*: https://law.justia.com/cases/california/court-of-appeal/3d/217/325.html ; Allen Matkins: https://allenmatkins.com/real-ideas/legal-alert-employees-of-unlicensed-subcontractor-may-assert-a-wage-claim-against-the-general-contractor.html
- [L20] B&P 7031: CACI 4561: https://www.justia.com/trials-litigation/docs/caci/4500/4561.html ; *White v. Cridlebaugh*: https://law.justia.com/cases/california/court-of-appeal/2009/f053843/ ; *MW Erectors*: https://scocal.stanford.edu/?p=33550
- [L21] LC 1776: https://codes.findlaw.com/ca/labor-code/lab-sect-1776/ ; LC 1771.4: https://codes.findlaw.com/ca/labor-code/lab-sect-1771-4/ ; LC 1775: https://california.public.law/codes/ca_lab_code_section_1775 ; Caltrans Labor Compliance Manual ch. 17: https://dot.ca.gov/programs/construction/labor-compliance/labor-compliance-manual/chapter-17 ; DIR eCPR FAQ: https://www.dir.ca.gov/Public-Works/ecprfaq.html
- [L22] Lien deadlines: https://www.huschblackwell.com/california-state-by-state-summary-of-construction-liens ; CSLB homeowner guide: https://cslb.ca.gov/Resources/GuidesAndPublications/HomeownersGuideToPreventingMechanicsLiens.pdf ; https://baylegal.com/california-mechanics-lien-deadlines-every-deadline-you-need-to-know/
- [L23] Statutory waivers (Civ. Code 8132-8138): https://cslb.ca.gov/consumers/legal_issues_for_consumers/Mechanics_Lien/Conditional_And_Unconditional_Waiver_Release_Form.aspx ; https://law.justia.com/codes/california/code-civ/division-4/part-6/title-1/chapter-3/section-8132/
- [L24] PAGA 2024 reform: https://www.cooley.com/news/insight/2024/2024-07-09-california-enacts-sweeping-changes-to-paga
- [L25] 2026 minimum wage and exempt threshold: https://www.dir.ca.gov/DIRNews/2025/2025-118.html ; https://www.thompsoncoburn.com/insights/new-california-2026-minimum-wage-will-also-increase-salary-threshold-for-many-exempt-employees/
- [L26] Gov. Code 12950.1 (CRD FAQ): https://www.dfeh.ca.gov/wp-content/uploads/sites/32/2022/11/Sexual-Harassment-Prevention-Training-For-Employers-FAQ_ENG.pdf
- [L27] B&P 6400 / 6411 (Legal Document Assistants): https://california.public.law/codes/business_and_professions_code_section_6400 ; https://california.public.law/codes/business_and_professions_code_section_6411
- [L28] CDI workers' comp advisory pure premium (Sept 1, 2026): https://www.insurance.ca.gov/0400-news/0100-press-releases/2026/release024-2026.cfm
- [L29] CSLB licensee counts: https://www2.cslb.ca.gov/Resources/BoardPackets/2026/FINAL_ANALYTICAL_STUDY_REPORT.pdf ; https://cslb.ca.gov/
- [L30] Wages: BLS San Diego May 2024: https://www.bls.gov/regions/west/news-release/2025/occupationalemploymentandwages_sandiego_20250501.htm ; BLS California May 2023: https://www.bls.gov/oes/2023/may/oes_ca.htm ; Salary.com: https://www1.salary.com/CA/San-Diego/Project-Coordinator-Salary.html ; Payscale: https://www.payscale.com/research/US/Job=Construction_Coordinator/Hourly_Rate/4d07b516/San-Diego-CA-Project-Coordinating
- [L31] Cal/OSHA Consultation (free): https://dir.ca.gov/DOSH/consmore.htm
- [L32] *Morton Engineering v. Patscheck* (7108.5 penalty): https://law.justia.com/cases/california/court-of-appeal/4th/87/712.html
- [L33] CSLB SB 1455 bulletin: https://www.cslb.ca.gov/Resources/IndustryBulletins/2024/SB%201455%20changes.FINAL.pdf ; 2026 new laws: https://www.cslb.ca.gov/Resources/IndustryBulletins/2026/New%20Laws.FINAL.pdf
- [L34] SB 779 summary (penalties from July 1, 2026): https://www.constructionowners.com/news/new-california-contractor-laws-take-effect-in-2026
- [L35] Electrician certification (LC 108): https://dir.ca.gov/dlse/ECU/ECUonline.html ; FAQ: https://dir.ca.gov/dlse/ECU/ecu_faq.htm

**Market and pricing**
- [S1] Procore pricing (OneCrew): https://www.getonecrew.com/post/procore-pricing
- [S2] Procore on Capterra: https://www.capterra.com/p/56250/Procore/pricing/
- [S3] Procore on Costbench: https://www.costbench.com/software/construction-management/procore/
- [S4] Buildertrend pricing (OneCrew): https://www.getonecrew.com/post/buildertrend-pricing
- [S5] Buildertrend (Downtobid): https://downtobid.com/blog/buildertrend-pricing
- [S6] Buildertrend on Capterra: https://www.capterra.com/p/70092/Buildertrend/pricing/
- [S7] Autodesk Build on Capterra: https://www.capterra.com/p/255145/Autodesk-Build/pricing/
- [S8] Autodesk Forma/ACC on G2: https://www.g2.com/products/autodesk-forma-formerly-autodesk-construction-cloud/pricing
- [S9] Levelset: https://levelset.com/pricing ; https://www.trustradius.com/compare-products/levelset ; https://www.softwareadvice.com/waiver/levelset-profile/
- [S10] Siteline: https://www.siteline.com/compare/levelset ; https://www.siteline.com/llms.txt
- [S11] Rhumbix acquired by Autodesk: https://www.enr.com/articles/62743-autodesk-acquires-trade-workforce-management-platform-rhumbix ; https://www.autodesk.com/blogs/construction/autodesk-acquires-rhumbix-to-help-bring-real-time-jobsite-data-into-project-costs/
- [S12] SafetyCulture pricing: https://safetyculture.com/pricing
- [S13] Highwire: https://g2.com/products/highwire-0/pricing ; https://www.highwire.com/pricing
- [S14] Billd: https://billd.com/subcontractors/ ; https://billd.com/faqs/
- [S15] CoConstruct retirement: https://coconstruct.com/migration ; https://projul.com/blog/coconstruct-shutdown-migration-guide/
- [S16] ServiceTitan: https://www.getonecrew.com/post/servicetitan-pricing ; https://projul.com/blog/servicetitan-pricing-analysis-2026/
- [S17] LCPtracker: https://lcptracker.com/solutions/lcpcertified/ ; https://prnewswire.com/news-releases/lcptracker-introduces-free-subscription-tier-for-single-contractor-certified-payroll-solution-300748819.html ; Paychex partnership: https://lcptracker.com/success/paychex
- [S18] Construction payroll: https://fitsmallbusiness.com/best-construction-payroll-software ; https://peoplemanagingpeople.com/tools/best-payroll-software-for-construction/ ; https://www.capterra.com/p/88851/Certified-Payroll/ ; https://www.softwareadvice.com/compliance/hcm-tradeseal-profile/
- [S19] COI tracking (Billy guide): https://billyforinsurance.com/resources/best-coi-tracking-software-construction/
- [S20] JobTread: https://www.jobtread.com/pricing ; 2026 pricing guide (Fieldwire, Contractor Foreman): https://projul.com/blog/construction-software-pricing-guide-2026/
- [S21] Protekon: https://protekon.vercel.app/solutions/construction ; https://protekon.vercel.app/blog/protekon-vs-safety-consultants-managed-compliance-compared
- [S22] Procore Groundbreak 2025 AI: https://www.businesswire.com/news/home/20251015796723/en/Procore-Advances-the-Future-of-Construction-with-New-AI-Innovations-at-Groundbreak-2025 ; Composio Procore MCP: https://composio.dev/toolkits/procore_core
- [S23] Autodesk MCP servers: https://www.autodesk.com/solutions/autodesk-ai/autodesk-mcp-servers ; Revit public MCP: https://www.autodesk.com/blogs/aec/2026/06/17/revit-public-mcp-server/
- [S24] Billd 2026 Subcontractor Market Report: https://billd.com/blog/billds-sixth-annual-national-subcontractor-market-report-reveals-the-playbook-behind-the-industrys-fastest-growing-subcontractors/
- [S25] Siteline 2026 State of Subcontractor Billing: https://www.siteline.com/digital-download/the-state-of-subcontractor-billing-in-2026 ; https://www.siteline.com/blog/92-percent-of-subcontractors-floated-payroll-last-year-new-siteline-report-finds
