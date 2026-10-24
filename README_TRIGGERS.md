# ClaimCare claim-history triggers

This change keeps the existing application structure and all table definitions. The SQL file `mysql_claim_history_triggers.sql` creates two MySQL triggers on the existing `claims` table:

- `AFTER INSERT`: writes the initial claim-history event using `created_by_user_id`.
- `AFTER UPDATE`: writes a history event only when `status` changes. It reads `@claimcare_actor_user_id` and `@claimcare_history_note`, which the Flask service sets on the same database transaction/connection before a status update.

The application-level `record_history()` calls for claim creation and status transitions have been removed to avoid duplicate status-history rows. Application-level history remains for edit and checklist events, because those are not handled by these triggers.

## Apply safely

1. Back up the existing database.
2. Confirm the current schema has the columns used by the script: `claims.id`, `claims.status`, `claims.created_by_user_id`, `claims.officer_user_id`; and `claim_history.claim_id`, `actor_user_id`, `from_status`, `to_status`, `event_type`, `note`, `timestamp`.
3. In MySQL Workbench, select the same database used by the app and run `mysql_claim_history_triggers.sql`.
4. Check with `SHOW TRIGGERS;`.
5. Test by creating a draft claim and changing its status. Confirm exactly one trigger-generated history row per creation/status transition.

Do not run any table-drop, table-recreate, seed, or reset command against your existing database. The trigger script drops/recreates only these two named triggers.

Note: MySQL triggers fire for writes from SQLAlchemy as well as other SQL clients. If your live schema differs from the model fields above, stop and reconcile the SQL to the actual schema before applying it.
