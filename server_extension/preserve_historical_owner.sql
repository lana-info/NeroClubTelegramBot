-- Known historical licenses whose original codes are unavailable.
-- Prevent automatic duplicate generation; do not rotate keys or alter devices.
DO $$
BEGIN
  IF (SELECT count(*) FROM licenses
      WHERE id IN ('db9302a7-8eba-3fee-01d7-47a7358ecfc2',
                   '891511a6-c68a-45db-7625-9fc830bcfc4f')) <> 2 THEN
    RAISE EXCEPTION 'Historical owner licenses require review before deployment';
  END IF;
END $$;
INSERT INTO club_vip_grants(telegram_id,product_id,license_id,encrypted_key,managed)
SELECT 591446297, product_id, id, NULL, false FROM licenses
WHERE id IN ('db9302a7-8eba-3fee-01d7-47a7358ecfc2',
             '891511a6-c68a-45db-7625-9fc830bcfc4f')
ON CONFLICT(telegram_id,product_id) DO NOTHING;
