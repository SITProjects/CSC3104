-- Seed a test user and a test admin without overwriting existing accounts.
--   user@example.com  / password123  (USER)
--   admin@example.com / admin12345   (ADMIN)
-- These passwords are public, so change or delete both accounts on any
-- coordinator reachable from outside the team.
INSERT INTO users (email, password_hash, role) VALUES
    ('user@example.com',
     'scrypt$16384$8$1$ZLSZzA4HigI8S0Ii4ay74A==$G03egcQkvZa+Avr/fQNVa2upTnS5NCHU1YG73KcN2mk=',
     'USER'),
    ('admin@example.com',
     'scrypt$16384$8$1$brY76jIBwHGxwAYLk032Ow==$KSDAmlX9Bwn+y5pUh780B7TgPUTLBifRycs9voJiLVg=',
     'ADMIN')
ON CONFLICT (email) DO NOTHING;
