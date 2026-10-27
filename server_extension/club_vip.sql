-- Additive integration schema. Existing licenses and devices are untouched.
CREATE TABLE IF NOT EXISTS club_vip_products (
    product_id uuid PRIMARY KEY REFERENCES products(id),
    bundle_id uuid REFERENCES access_bundles(id),
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS club_vip_grants (
    telegram_id bigint NOT NULL,
    product_id uuid NOT NULL REFERENCES products(id),
    license_id uuid NOT NULL UNIQUE REFERENCES licenses(id),
    encrypted_key text,
    managed boolean NOT NULL,
    suspended_by_club boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_id, product_id)
);
INSERT INTO club_vip_products(product_id,bundle_id)
SELECT p.id,b.id FROM products p
LEFT JOIN access_bundles b ON b.product_id=p.id AND b.code='club-vip'
    AND b.status='active' AND b.mode='managed'
WHERE p.code IN ('clipart-generator','trendwatcher-video') AND p.status='active'
ON CONFLICT(product_id) DO NOTHING;
