# mongodb_sandbox

A blank MongoDB 8 instance. No collections, no seed data — create your own.

## Run

```bash
docker compose up -d
```

## Connect

| | |
|---|---|
| host | `localhost` |
| port | `27017` |
| user | `root` |
| password | `root` |
| auth database | `admin` |

Shell inside the container (no client install needed):

```bash
docker compose exec mongo mongosh -u root -p root
```

From the host, if you have `mongosh` installed:

```bash
mongosh "mongodb://root:root@localhost:27017/sandbox?authSource=admin"
```

## First five minutes

Databases and collections spring into existence on first write — there is no
`CREATE DATABASE`. Paste this into `mongosh`:

```javascript
use sandbox

db.users.insertMany([
  { name: "Ada",   role: "eng",   age: 36, tags: ["founder"] },
  { name: "Linus", role: "eng",   age: 54, tags: ["kernel", "git"] },
  { name: "Grace", role: "eng",   age: 45, tags: ["compiler"] },
  { name: "Mai",   role: "sales", age: 30 }
])

db.users.find({ role: "eng" })                    // filter
db.users.find({ age: { $gt: 40 } })               // operators are $-prefixed
db.users.find({ tags: "git" })                    // arrays match element-wise
db.users.find({}, { name: 1, _id: 0 })            // projection

db.users.updateOne({ name: "Mai" }, { $set: { role: "eng" } })
db.users.updateMany({ role: "eng" }, { $inc: { age: 1 } })
db.users.deleteOne({ name: "Grace" })

// Aggregation pipeline: stages run in order, each feeding the next.
db.users.aggregate([
  { $match: { role: "eng" } },
  { $group: { _id: "$role", count: { $sum: 1 }, avgAge: { $avg: "$age" } } }
])

db.users.createIndex({ name: 1 })
db.users.find({ name: "Ada" }).explain("executionStats")   // did it use the index?

show dbs
show collections
db.users.countDocuments()
db.users.drop()
```

## Notes

- Every document gets an `_id` (an `ObjectId`) if you don't supply one. It is the
  primary key and is indexed automatically.
- Documents in one collection need not share a shape — `Mai` above has no `tags`
  field and that is legal. Schema discipline is your job, not the server's.
- Data lives in the named volume `mongodb-sandbox_mongodata`, so it survives
  `docker compose down`.
- Override any setting by copying `.env.example` to `.env`.

## Wipe everything

```bash
docker compose down -v
```
