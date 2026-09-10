# Fan-out on write — the filtering has to happen somewhere

**The claim you are practising:** *"One shared stream means every connected
client is woken by every event and runs the authorization check itself. That
scales with concurrency, not with traffic — and to decide Bob may not see the
event, Bob's process had to receive it."*

## Measured on this machine

```
connections listening:              50
connections woken by ONE event:     50
authorization checks run:           50
events actually delivered:           1
time queuing for a pool of 10:    620ms across 50 checks
```

49 of those checks existed only to say no.

## The two costs, and only one of them is about performance

### 1. It scales with idle users

Ten thousand connected users make one publish ten thousand times more expensive.
The graph you see is **CPU rising while requests-per-second stays flat**, which
is a genuinely confusing shape to debug.

And the checks do not merely burn CPU — they **queue**. They share a connection
pool with every other request the service is serving, so one publish to a shared
stream is a burst of N queries against a pool sized for your steady state. *The
endpoint that gets slow is not the feed.*

### 2. The confidentiality guarantee is an `if` statement

Read the naive listener again: to decide Bob may not see the payroll event,
**Bob's connection had to receive the payroll event**. Its name was in his
process's memory. It was one `logger.debug(fields)` away from your log
aggregator, one exception handler away from Sentry, and one refactor away from
being yielded.

Four different people will edit that function this year.

## Fan-out on write

One query answers *who may see this* — the policy run **backwards** — and the
event is appended only to those mailboxes:

```
POST /events/11?mode=fanout
  delivered_to:  [1]
  withheld_from: [2, 3, 4]
  reasons: {1: "allowed: explicit grant", 2: "denied: restricted, no grant",
            3: "denied: different org",   4: "denied: restricted, no grant"}

entries in Alice's stream: 1
entries in Bob's stream:   0
```

That is a stronger claim than "Bob's client filtered it out", and it is stronger
in the way that survives a bad refactor: **there is no code path that could leak
it, because there is no copy of it to leak.** The guarantee moved from a runtime
branch into the shape of the data.

> Note the requirement this creates. Fan-out needs the policy answered
> *backwards*: not "may this subject see this row" but "which subjects may".
> Every policy engine answers the first. If yours cannot answer the second,
> fan-out on write is not available to you at any price.

## The cost nobody mentions: fan-out freezes the decision

The mailbox **is** the decision — a durable record of what somebody was allowed
to see at one instant. Permissions do not hold still.

| What you do | What happens |
|---|---|
| Grant Bob access *after* the event | It is not in his mailbox and **never will be**. Fan-out cannot deliver backwards. |
| Revoke Alice's access *after* delivery | It is **still in her mailbox**, readable until MAXLEN trims it — and she can reconnect with `Last-Event-ID: 0-0` and read it again tomorrow. |

Both are proven in the lab. For a chat app nobody minds. For *"you've been added
to the payroll project, here's its history"* the first is a missing feature that
looks like a bug and is actually an architecture. For the second, your
revocation latency is a **retention setting nobody chose with security in mind**.

This is the same lesson as `redis_cache_lab`'s scenario 05 — *"revoked at 14:02"
means "loses access when the TTL expires"* — moved onto the push side.

## The resolution: fan out pointers, authorize the dereference

The two properties belong to different things:

- **Routing** may be decided at write time. It is the cheap part, and being
  slightly wrong costs a wasted mailbox entry.
- **Content** must be authorized at read time. It is the part where being wrong
  is a disclosure.

So the mailbox holds an id, the body lives once under its own key, and the
reader re-runs the policy when it exchanges one for the other:

```
pointer entries in Alice's mailbox:   1
pointer entries in Bob's mailbox:     0     ← no herd: he is never woken
frames she received before revoke:    1
frames she receives after revoke:     0     ← the re-check caught up
```

**The honest costs:** one extra round trip per event on the read path; a body
whose retention must outlive every pointer that names it, or you serve gaps; and
the policy now runs in two places, so it **must be one function called twice**
(`app/usecases/authorize.py`). A policy that exists twice is a policy that will
be enforced once — scenario 04 has the test that keeps it honest, and it exists
because these two *did* drift while this lab was being written.

## Storage amplification

```
audience:                    203 subscribers
fan-out of the full body:    19,488 bytes
fan-out of pointers:          3,750 bytes      (5.2x less)

at 10k subscribers, 1 KB events, 100 events/day:
    full body   1.0 GB/day copied
    pointers    0.02 GB/day
```

Storage amplification is *event size × audience*, and Redis Streams live in RAM.
That number decides whether fan-out on write is even available to you.

## So which one?

| Situation | Design |
|---|---|
| small audience, cheap events, stable permissions | fan out the body |
| large audience, or permissions that change | fan out pointers |
| audience of one (a personal notification) | they are the same thing |
| genuinely public data | one shared stream is correct — nothing to leak |

Neither by reflex.

## Run it

```bash
./lab.sh run 02
./lab.sh streams          # see whose mailbox holds what
./lab.sh publish 11 fanout
./lab.sh publish 11 pointer
```

## The interview answer

> *"You have a realtime feed with per-user permissions. One stream or many?"*

"Many, but not for the reason people usually give. One shared stream means every
connected client is woken by every event and runs the check itself — that scales
with concurrency rather than traffic, so 50 listeners turn one event into 50
checks to make one delivery, and those checks queue on the same pool as every
other request. And it isn't only a CPU argument: to decide Bob may not see the
event, Bob's process had to receive it, so confidentiality rests on an `if`
statement rather than on the data's shape.

So I fan out on write — one query answers who may see it, and the event is never
written where it may not go. The cost people leave out is that fan-out *freezes*
the decision: granting access later can't deliver backwards, and revoking doesn't
un-deliver. Where that matters I fan out ids rather than bodies and re-authorize
at the dereference — routing at write time, content at read time — which also
drops the storage amplification from a copy per subscriber to a pointer."
