# Android WhatsApp recovered key UI

- Automatic offline decryption after Android extraction is unchanged; no manual decrypt button was added.
- Completed Android acquisition displays key availability per crypt15 directory. An explicit Reveal key / Hide key control shows the acquired matching 32-byte key as 64 hex characters.
- A key is never created or guessed from ciphertext. No ADB call is issued by the read-only UI endpoint.
- The endpoint requires forensic.job.create and the original examiner identity or tenant-admin role, and limits reads to the server-selected acquisition original tree. Key bytes are not added to routine run JSON or audits.
- The key is sensitive: revealing it gives access to the underlying chat database. Secure device/browser access and TLS are essential.
- Host-helper jobs and runs not available in the acquisition worker do not support reveal through this endpoint.
- The client may retain the revealed value in memory until Hide, navigation, or reload; no local-storage persistence was added.
- The project has no frontend node_modules in this environment, so the production frontend build has not been verified.
