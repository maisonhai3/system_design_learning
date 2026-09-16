# 01 — One order, end to end

```bash
./lab.sh scenario 01
```

Three orders go in. One is confirmed, one is declined, one is paid for and then
cancelled because the warehouse turns out to be empty.

## What to notice

**Every line says where it came from.** `orders/0@0003` is topic `orders`,
partition 0, offset 3. Get used to reading these — almost every Kafka problem
becomes obvious once you can see which partition a record landed on and how far
each consumer has got through it.

**The gateway answers 202 Accepted, not "confirmed".** It replies as soon as the
order is durably on the log, before anyone has tried to charge for it. That
buys a fast write that stays up even when payment is down, and it costs you the
ability to tell the user "declined" in the HTTP response. If your product needs
that answer synchronously, no amount of Kafka will give it to you, and you
should make the call synchronously instead. This is a product decision wearing
an architecture costume.

**Three services, one order, no coordinator.** Payment does not call inventory.
It publishes `PaymentCompleted` and stops caring. Inventory consumes the
*payments* topic — so "reserve stock only for orders that are paid" is
expressed by what it subscribes to, not by an if-statement. Rewiring the
business process means rewiring subscriptions.

**Watch the third order carefully.** `doohickey` is paid for, then rejected, then
refunded. Inventory has never heard of refunds; it publishes "I rejected that
stock" as a fact, and payment is listening. That is a saga — a multi-step
transaction where the undo is another event rather than a rollback, because no
database spans these three services.

**Nothing was consumed away.** The dump at the end reads the same records the
services already processed. In RabbitMQ or SQS, reading removes; here it does
not. That single difference is what makes scenarios 04 and 05 possible.

## Try it yourself

```bash
./lab.sh order widget 11        # more than the shelf holds
./lab.sh order gizmo 1 broke    # the payment service knows this customer
./lab.sh dump stock             # inventory's decisions, on the log
```

## Question to sit with

The gateway rejects an unknown item, but has no opinion on whether you can
afford it or whether it is in stock. Why is that the right split — and what
breaks if the gateway checks stock before accepting?

<details>
<summary>One answer</summary>

To check stock, the gateway would have to ask inventory, synchronously, on the
request path. Now a slow or down inventory service makes the gateway slow or
down, and you have coupled availability of the front door to the availability of
a back-office service. You would also be lying: stock can be gone by the time
payment finishes, so the check buys you a nicer error message most of the time
and a correctness guarantee never.

The gateway validates what it alone owns — does this item exist, is the
quantity sane — and lets every other decision happen where the data lives.
</details>
