-- Idempotent seed. `./lab.sh reset` re-runs 01 + 02 in about a second, and
-- every run.py calls reset() first, so scenarios never contaminate each other.

TRUNCATE dataset_grants, datasets, users RESTART IDENTITY CASCADE;

INSERT INTO users (id, email, display_name, role, org_id) VALUES
    (1, 'alice@acme.test',  'Alice',  'guest',  100),
    (2, 'bob@acme.test',    'Bob',    'member', 100),
    (3, 'carol@globex.test','Carol',  'admin',  200),
    (4, 'dan@globex.test',  'Dan',    'member', 200);

INSERT INTO datasets (id, org_id, name, classification) VALUES
    (10, 100, 'acme/clickstream-2026',   'internal'),
    (11, 100, 'acme/payroll-2026',       'restricted'),
    (12, 100, 'acme/public-benchmarks',  'public'),
    (13, 200, 'globex/sensor-telemetry', 'internal'),
    (14, 200, 'globex/hr-incidents',     'restricted');

-- Alice may see acme's restricted payroll. Bob may not. That single row is
-- what scenario 05 revokes — and what a badly-keyed cache leaks.
INSERT INTO dataset_grants (user_id, dataset_id) VALUES
    (1, 11),
    (3, 14);
