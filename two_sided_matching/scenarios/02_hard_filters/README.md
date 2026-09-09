# 02 — Hard filters, and what NULL means

**The claim:** every hard filter is a bet that a column is populated, and on
real job data most of them are not. Before you write `WHERE salary_max >=
:floor`, find out what fraction of your rows have a `salary_max` at all.

## The numbers this corpus actually has

Scraped from Hacker News, Remote OK and Arbeitnow — 1,865 real postings:

```
  column            populated   coverage
  salary_max              314     16.8%
  country                1424     76.4%
  min_years_exp           944     50.6%
    of which inferred     634     34.0%     <- guessed from a title word
  sponsors_visa           126      6.8%
  employment             1865    100.0%
```

**17% state a salary.** A strict salary filter is therefore mostly a filter on
"did anyone bother to write it down". And a third of the experience
requirements were not stated either — the pipeline inferred them from the word
"senior" in the title, which is why `job_docs.parsed.years_inferred` exists:
a requirement you guessed and a requirement they published should not be
enforced with the same confidence.

## Three answers, and none of them is free

```
  candidate         off   lenient   strict   strict/off
  Priya Iyer        400        88       12          3%
  Yuki Tanaka       400        25        2          1%
  Hao Yang          400       202       32          8%

  summed over 9 candidates:   off 3600   lenient 1058   strict 71
```

`strict` (unknown disqualifies) keeps **2%** of the retrieved pool.
**14 of 60 candidates get an empty page.** Not because no job suits them —
because nobody wrote the salary down.

`lenient` (unknown passes) keeps the market and will happily recommend a job
that turns out to pay half what the candidate needs.

There is no third option where the missing data appears. Pick one, and know
which one you picked. This lab defaults to `lenient` and shows the unknowns.

## The filter must be a gate, not a ranking signal

The tempting shortcut is to skip filtering and let a low preference score push
ineligible jobs down. Measured, with eligibility as a soft signal only:

```
  Yuki Tanaka       7/10 of the top 10 were ineligible
  Tuan Jones        9/10
  across 9 candidates: 44 ineligible results in 90 slots
```

A job the candidate cannot legally take is not a slightly worse job. Gate it,
and the leak is zero by construction.

## What to show when the gate rejects everything

"No results" tells the user their profile is broken and they leave. The
rejection reasons tell them which knob to turn:

```
  Commercial Building Estimator     salary not stated; needs 8y, candidate has 1.1y
  Founding Engineer                 salary not stated; candidate is remote-only, job is not
  Remote (US only)                  salary not stated; needs 3y, candidate has 1.1y
```

Now the UI can say *"4 jobs match your skills but none states a salary — show
them anyway?"* That is a product feature that falls out of writing the filter
as a function returning reasons rather than a boolean.

## One modelling decision worth arguing about

`remote` does not mean "location no longer applies". A Japan-based candidate is
correctly *not* shown a US-remote role, because time zone and right-to-work do
not evaporate when the job is remote. This single decision moves a lot of
results — flip it in `match/engine.py:eligibility` and watch.

```bash
./lab.sh doctor
./lab.sh run 02
./lab.sh suggest-jobs 4 --filters strict     # vs --filters lenient, --filters off
```
