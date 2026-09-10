-- Idempotent seed. `./lab.sh reset` re-runs 01 + 02 in about a second.

TRUNCATE feed_deliveries, dataset_grants, datasets, users CASCADE;

INSERT INTO users (id, email, display_name, role, org_id) VALUES
    (1, 'alice@acme.test',   'Alice', 'admin',  100),
    (2, 'bob@acme.test',     'Bob',   'member', 100),
    (3, 'carol@globex.test', 'Carol', 'admin',  200),
    (4, 'dan@acme.test',     'Dan',   'guest',  100);

INSERT INTO datasets (id, org_id, name, classification) VALUES
    (10, 100, 'acme/clickstream-2026',      'internal'),
    (11, 100, 'acme/payroll-q3-CONFIDENTIAL','restricted'),
    (12, 100, 'acme/public-benchmarks',     'public'),
    (13, 200, 'globex/sensor-telemetry',    'internal'),
    (14, 200, 'globex/hr-incidents',        'restricted');

-- Alice may see acme's restricted payroll. Bob may not. Dan is a guest and
-- sees only public rows. That one grant row is the whole plot of this lab:
-- an event about dataset 11 must reach Alice's feed and must never appear in
-- Bob's.
INSERT INTO dataset_grants (user_id, dataset_id) VALUES
    (1, 11),
    (3, 14);
