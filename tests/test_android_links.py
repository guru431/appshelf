import pytest

from appshelf.android import links, net

K9, VLC, VTB, SBER = "com.fsck.k9", "org.videolan.vlc", "C102312137", "ru.sberbankmobile"


@pytest.mark.parametrize("url, expected", [
    ("https://play.google.com/store/apps/details?id=com.fsck.k9", ("play", K9)),
    ("https://play.google.com/store/apps/details?id=com.fsck.k9&hl=ru&gl=RU", ("play", K9)),
    ("https://play.google.com/store/apps/details/?hl=ru&id=com.fsck.k9", ("play", K9)),
    ("https://appgallery.huawei.com/app/C102312137", ("appgallery", VTB)),
    ("https://appgallery.huawei.com/app/C102312137?sharePrepath=ag&locale=ru_RU&source=appshare", ("appgallery", VTB)),
    ("https://appgallery.huawei.com/#/app/C102312137", ("appgallery", VTB)),
    ("https://appgallery.cloud.huawei.com/marketshare/app/C102312137?locale=ru_RU", ("appgallery", VTB)),
    ("https://appgallery.cloud.huawei.com/ag/n/app/C102312137/", ("appgallery", VTB)),
    ("C102312137", ("appgallery", VTB)),
    ("https://f-droid.org/packages/org.videolan.vlc/", ("fdroid", VLC)),
    ("https://f-droid.org/en/packages/org.videolan.vlc/", ("fdroid", VLC)),
    ("https://f-droid.org/ru/packages/org.videolan.vlc", ("fdroid", VLC)),
    ("https://f-droid.org/app/org.videolan.vlc", ("fdroid", VLC)),   # «Поделиться» в клиенте F-Droid
    ("https://www.f-droid.org/packages/org.videolan.vlc/?locale=ru", ("fdroid", VLC)),
    ("https://apkpure.com/k-9-mail/com.fsck.k9", ("apkpure", K9)),
    ("https://m.apkpure.com/k-9-mail/com.fsck.k9/download", ("apkpure", K9)),
    ("https://apkpure.com/ru/k-9-mail/com.fsck.k9/", ("apkpure", K9)),
    ("https://apkpure.net/k-9-mail/com.fsck.k9?hl=ru", ("apkpure", K9)),
    ("https://www.rustore.ru/catalog/app/ru.sberbankmobile", ("rustore", SBER)),
    ("https://rustore.ru/catalog/app/ru.sberbankmobile/", ("rustore", SBER)),
    ("https://apps.rustore.ru/app/ru.sberbankmobile", ("rustore", SBER)),
    ("  https://f-droid.org/packages/org.videolan.vlc/\n", ("fdroid", VLC)),
])
def test_store_links_recognized(url, expected):
    assert links.parse(url) == expected


def test_unknown_page_is_direct_link():
    for url in ("https://bank.example/app.apk", "https://f-droid.org/repo/org.videolan.vlc_13070206.apk",
                "https://appgallery.huawei.com/", "https://apkpure.com/"):
        assert links.parse(url) == ("link", url)
    with pytest.raises(net.FetchError, match="https"):
        links.parse("http://bank.example/app.apk")
    with pytest.raises(net.BlockedAddress):
        links.parse("https://192.168.0.1/app.apk")


@pytest.mark.parametrize("url", [
    "https://play.google.com/store/apps/details?id=../x",
    "https://play.google.com/store/apps/details",
    "https://appgallery.huawei.com/app/C12",
    "https://appgallery.huawei.com/app/X102312137",
    "https://f-droid.org/en/packages/nodots/",
    "https://f-droid.org/packages/..%2Fx/",
    "https://apkpure.com/name/com..x",
    "https://www.rustore.ru/catalog/app/bad..pkg",
])
def test_bad_package_in_store_link_refused(url):
    with pytest.raises(net.FetchError, match="не похоже на страницу приложения"):
        links.parse(url)
