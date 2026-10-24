-- ClaimCare claim-history triggers
-- Run this against the EXISTING ClaimCare database after backing it up.
-- This script changes triggers only; it does not alter or recreate any table.

DROP TRIGGER IF EXISTS trg_claims_after_insert_history;
DROP TRIGGER IF EXISTS trg_claims_after_status_update_history;

DELIMITER $$

CREATE TRIGGER trg_claims_after_insert_history
AFTER INSERT ON claims
FOR EACH ROW
BEGIN
    INSERT INTO claim_history
        (claim_id, actor_user_id, from_status, to_status, event_type, note, timestamp)
    VALUES
        (NEW.id, NEW.created_by_user_id, NULL, NEW.status, 'status_change',
         'Claim created', UTC_TIMESTAMP());
END$$

CREATE TRIGGER trg_claims_after_status_update_history
AFTER UPDATE ON claims
FOR EACH ROW
BEGIN
    IF NOT (OLD.status <=> NEW.status) THEN
        INSERT INTO claim_history
            (claim_id, actor_user_id, from_status, to_status, event_type, note, timestamp)
        VALUES
            (NEW.id,
             COALESCE(@claimcare_actor_user_id, NEW.officer_user_id, NEW.created_by_user_id),
             OLD.status,
             NEW.status,
             'status_change',
             COALESCE(@claimcare_history_note,
                      CONCAT('Status changed from ', OLD.status, ' to ', NEW.status)),
             UTC_TIMESTAMP());
        SET @claimcare_actor_user_id = NULL;
        SET @claimcare_history_note = NULL;
    END IF;
END$$

DELIMITER ;
