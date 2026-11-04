-- Non-secret user-confirmed local policy, never reusable credentials.
CREATE TABLE local_policy (name TEXT PRIMARY KEY, payload TEXT NOT NULL);
ALTER TABLE accounts ADD COLUMN local_removed INTEGER NOT NULL DEFAULT 0 CHECK(local_removed IN (0,1));
CREATE TABLE account_revocations (account_id TEXT NOT NULL, epoch INTEGER NOT NULL, snapshot TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account_id,epoch));
INSERT INTO account_revocations(account_id,epoch,snapshot) SELECT account_id,epoch,snapshot FROM accounts WHERE json_extract(snapshot,'$.revocation_pending')=1;
