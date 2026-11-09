# APK signature fixtures

Tiny synthetic applications (`ru.example.app`), signed by temporary RSA-2048 keys with the official Android SDK
Build Tools `apksigner`. These are protocol fixtures, not installable application functionality. No private key
or keystore is included. Certificate subjects contain only `CN=Appshelf Test Fixture`.

`v1-{5,6}.apk`, `v2-{5,6}.apk`, and `v3-{5,6}.apk` contain complete real signatures for their respective scheme
and versionCode. `v3-rotated-6.apk` uses a genuine v3 proof-of-rotation from the original to a second key,
with the original key's installed-data capability enabled. Tests mutate both APK contents and the signed
rotation proof; retaining the same extracted certificate is insufficient to pass verification.
`v31-rotated-6.apk` is the same rotation signed with `--rotation-min-sdk-version 33` (v3.1): the original key
signs for API 24–32, the rotated key for API 33 and later.

Original certificate SHA-256: `6cb0d638923096bfd175cfa91e16404980f5f706eb8dfde5ba37e5528040f186`.
Rotated certificate SHA-256: `e08de9a9f6987978aafa11b24c4276f7146e72c29306bf16dd99bebbbe561d65`.

Run `tests/test_apk_verification.py` with the `integration` marker enabled and a system `apksigner` on PATH,
or set `APPSHELF_APKSIGNER` to an SDK `apksigner.jar` (Java is required). The ordinary pipeline tests use only
immutable hashes registered by `apk_factory.make_apk`; this module always uses the actual AOSP verifier.

Reference: [official apksigner commands](https://developer.android.com/tools/apksigner).
