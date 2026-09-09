# 03 — Reciprocal ranking

**The claim:** once you have two directional scores, the function that combines
them is a design decision with measurable consequences, and the obvious choice
is wrong.

## Harmonic, not arithmetic

```
  candidate wants   employer wants   arithmetic   harmonic   verdict
  0.90              0.90             0.90         0.90       both keen
  0.60              0.60             0.60         0.60       both lukewarm
  1.00              0.20             0.60         0.33       employer says no
  0.05              0.95             0.50         0.10       hopeless
```

Rows 2 and 3 average identically. Only one of them is a match. The harmonic
mean is dominated by its smaller argument, which is the arithmetic of "both
parties have to say yes".

## What it is worth

Counting top-10 slots where *both* sides clear a modest bar:

```
  arithmetic mean    97/110   (88%)
  harmonic mean     106/110   (96%)
```

Eight percentage points of wasted recommendations, for one line of arithmetic.
On a real marketplace those slots are applications that get screened out and
recruiter time spent on candidates who were never going to accept.

## The same trap, one level down

This applies *inside* each direction too, and that is the easier one to miss —
this lab shipped it wrong first. See `01_symmetry_trap/README.md`; the fix
there is a geometric mean over each side's components, so a dealbreaker cannot
be averaged away by unrelated positives.

Two levels, two combiners, same principle: **the mean you reach for by reflex
cannot express "no".**

## The disagreement is real and large

Widest gaps inside one candidate's *eligible* set — these are all jobs she
could legally take:

```
  job                                  they want it   employer wants them   gap
  Rust Backend Engineers & Full-Stack  1.00           0.17                  0.83
  Senior SWE Backend                   1.00           0.37                  0.63
```

A gap of 0.83 between the two readings of the same pair. Averaging that into
one number discards the most informative thing you computed.

```bash
./lab.sh run 03
./lab.sh suggest-jobs 4        # the wants→ / ←wanted columns are the two directions
```
