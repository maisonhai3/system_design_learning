# 07 — ANN recall and the filter trap

**The claim:** the obvious pgvector query is wrong in a way that returns
plausible results instead of an error, and it only starts being wrong once your
table is big enough that you have stopped looking.

## Unfiltered, HNSW is a good trade

```
  ef_search   recall@20   latency
  10             71.5%     0.31 ms
  40             89.5%     0.36 ms
  100            96.0%     0.47 ms
  400            99.5%     1.02 ms
```

The default `ef_search = 40` is a guess about your data, not a good value.
Measure this curve; it costs ten minutes.

Build it with `vector_cosine_ops` to match the `<=>` operator. Build it with
`vector_l2_ops` and the planner will not use it for `<=>` at all — no error, no
warning, just the sequential scan you were trying to avoid, now with an index
to maintain.

## Add a WHERE clause, and Postgres saves you

```sql
SELECT d.job_id FROM job_docs d JOIN jobs j ON j.id = d.job_id
WHERE j.status = 'open' AND j.remote AND j.salary_max >= 150000
ORDER BY d.embedding <=> $vec LIMIT 20;
```

At 1,865 rows the planner **does not use the HNSW index at all**. With
`salary_max` indexed and 5% of rows matching, a bitmap scan followed by an exact
sort of the survivors is cheaper — and exact. Postgres found the right plan
without help.

Nothing is wrong here, and that is the problem: **this is the test that passes
before the bug.**

## The same query at 30x the rows

Inflate to ~56,000 vectors. Now the planner picks `Index Scan using
scale_hnsw`:

```
  ef_search   rows returned   recall@20   latency
  40                4/20         20.0%     0.57 ms
  100              20/20         80.0%     1.04 ms
  400              20/20         95.0%     1.92 ms
```

**Four rows for `LIMIT 20`, while 20 qualifying rows exist.**

HNSW walks the graph in distance order and the filter is applied to whatever
comes back. The index knows nothing about the filter column, so it offers its
`ef_search` nearest neighbours, the filter discards most of them, and the query
runs out. No error is raised. A short result set is indistinguishable from
"there were not many matches", which is exactly why this ships.

## Three fixes

```
  approach                   rows    recall@20   latency    what it costs
  ANN, ef_search=40           4/20      20.0%     0.57 ms   silently short
  ANN, ef_search=400         20/20      95.0%     1.92 ms   10x graph walk, still approximate
  pre-filter CTE + exact     20/20     100.0%     7.25 ms   scans the filtered set
  partial index on tier=3    20/20     100.0%     0.78 ms   one index per filter combination
```

### The CTE has its own trap

```
  same CTE without AS MATERIALIZED     4/20 rows — inlined, back on the index
  with AS MATERIALIZED                20/20 rows
```

Before PostgreSQL 12 a CTE was always an optimisation fence. Since 12 a plain
one is **inlined**, so the planner flattens it straight back into the filtered
query and picks HNSW again — returning the same 4 rows the fix was supposed to
fix. The first version of this scenario had exactly that bug, and it read as
"the pre-filter idea is wrong" rather than "the SQL is wrong".

## The rule

An ANN index answers *"what is nearest"*, not *"what is nearest among rows
satisfying P"*.

- **Selective P** → filter first (materialised CTE), search exactly. The
  eligible set is small, so the exact scan is cheap — the index is least useful
  in precisely the case where it is most wrong.
- **Loose P** → search approximately, filter after, and raise `ef_search`.
- **One filter combination dominating your traffic** → partial index. Only
  then: combinations multiply and each is an index to build and maintain.

And test against production-sized data, because the small-table plan is a
different plan and it is the correct one.

```bash
./lab.sh run 07
./lab.sh index build     # then re-run ./lab.sh suggest-jobs 4 --retrieval ann
```
