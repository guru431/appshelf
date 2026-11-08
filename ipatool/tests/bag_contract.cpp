// Offline contract for Apple's nested/flat bags and authentication-only routing.
#include <bits/stdc++.h>
#include <nlohmann/json.hpp>
#define private public
#include "appstore.h"
#undef private

static HttpResponse fixture;
static std::string requestedURL;
static std::map<std::string, std::string> requestedHeaders;

HttpClient::HttpClient(const std::string& file) : m_cookieFile(file) {}
HttpClient::~HttpClient() = default;
HttpResponse HttpClient::get(const std::string& url,
                            const std::map<std::string, std::string>& headers) const {
    requestedURL = url;
    requestedHeaders = headers;
    return fixture;
}

static void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}

int main() {
    try {
        const PlistDict fields = {
            {"authenticateAccount", PlistValue::makeString("https://auth.itunes.apple.com/auth/v1/native/fast")},
            {"redownloadProduct", PlistValue::makeString("https://buy.itunes.apple.com/redownload")},
            {"sign-sap-setup", PlistValue::makeString("https://buy.itunes.apple.com/setup")},
            {"sign-sap-setup-cert", PlistValue::makeString("https://buy.itunes.apple.com/cert")},
            {"sign-sap-version", PlistValue::makeInt(200)},
        };
        AppStore store;
        for (bool nested : {true, false}) {
            fixture = {200, encode_plist_xml(nested
                ? PlistDict{{"urlBag", PlistValue::makeDict(fields)}} : fields), {}};
            for (bool authentication : {false, true}) {
                const auto bag = store.fetch_bag_impl("000393123456", authentication);
                require(bag.sapVersion == 200 && !bag.signSapSetup.empty()
                        && !bag.signSapSetupCert.empty(), "flat/nested SAP configuration lost");
                require(bag.authEndpoint == "https://auth.itunes.apple.com/auth/v1/native/fast/",
                        "native authentication endpoint changed");
                require(bag.hardwareID == std::vector<uint8_t>({0, 3, 147, 18, 52, 86}),
                        "device identity changed");
                require(bag.redownloadEndpoint == "https://buy.itunes.apple.com/redownload",
                        "download configuration lost");
                require(requestedURL == std::string("https://init.itunes.apple.com/bag.xml?")
                        + (authentication ? "ix=6&guid=" : "guid=") + "000393123456",
                        "authentication settings leaked into the catalog request");
                require(requestedHeaders.contains("User-Agent") == authentication,
                        "authentication User-Agent leaked into the catalog request");
                if (authentication)
                    require(requestedHeaders.at("User-Agent") == CONFIGURATOR_UA,
                            "authentication User-Agent differs between bag and login");
            }
        }
        fixture = {204, "", {}};
        bool rejected = false;
        try { store.fetch_bag_impl("000393123456", true); }
        catch (const EdgeRejected&) { rejected = true; }
        require(rejected, "empty Apple rejection was accepted as configuration");
        std::cout << "OK: nested/flat bag, authentication isolation, empty rejection\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
