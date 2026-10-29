"""Живые AppGallery, F-Droid, APKPure (-m integration): сведения и разбор ответов, без скачивания APK (они по
50–190 МБ). Сеть — машины, где идут тесты; AppGallery с зарубежного адреса отвечает другой зоной — нужен российский."""
import pytest

from appshelf.android import apkpure, appgallery, fdroid, net

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def http():
    return net.Http()


def test_appgallery_vtb_live(http):
    app = appgallery.info(http, appgallery.HOST, "ru.vtb24.mobilebanking.android")
    assert app.app_id.startswith("C") and app.version_code > 0 and len(app.sha256) == 64 and app.size > 0
    assert appgallery.package_of(http, app.app_id) == "ru.vtb24.mobilebanking.android"


def test_fdroid_vlc_live(http):
    pkg = fdroid.info(http, "org.videolan.vlc")
    assert pkg.suggested in fdroid.candidates(pkg) and len(fdroid.candidates(pkg)) > 1   # сборки по ABI
    meta = http.head(fdroid.apk_url("org.videolan.vlc", fdroid.candidates(pkg)[0]))
    assert meta.status == 200 and meta.length


def test_apkpure_k9_and_whatsapp_live(http):
    k9 = apkpure.info(http, "com.fsck.k9")
    assert (k9.kind, k9.package) == ("apk", "com.fsck.k9") and k9.version_code > 0 and len(k9.sha1) == 40
    assert apkpure.info(http, "com.whatsapp").kind == "xapk"
