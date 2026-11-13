// Offline contract: proofs cannot override tokens, inject headers, or leak
// helper errors. Legacy requests without native state keep their old headers.
#include "native_commerce.h"
#include <iostream>

static void require(bool value,const char* message) {
    if (!value) throw std::runtime_error(message);
}
int main() {
    try {
        const auto proof=parse_native_store_proof(R"({"X-Apple-AMD":"proof","X-Apple-AMD-M":"device"})");
        require(proof.size()==2 && proof.at("X-Apple-AMD")=="proof","valid proof rejected");
        for (const auto* raw : {R"({"error":"secret"})",R"({"X-Apple-AMD":"proof","X-Apple-AMD-M":"device","X-Token":"foreign"})",
                R"({"X-Apple-AMD":"proof\r\nCookie: secret","X-Apple-AMD-M":"device"})",R"({"X-Apple-AMD":"","X-Apple-AMD-M":"device"})"}) {
            bool rejected=false;
            try { parse_native_store_proof(raw); } catch (const std::runtime_error& e) {
                rejected=std::string(e.what()).find("secret")==std::string::npos;
            }
            require(rejected,"unsafe proof accepted or leaked");
        }
        const std::map<std::string,std::string> legacy{{"X-Token","saved"}};
        unsetenv("HOME");
        require(native_commerce_headers("https://example.invalid",legacy)==legacy,"legacy request changed");
        setenv("HOME","/nonexistent-appshelf-contract",1);
        require(native_commerce_headers("https://example.invalid",legacy)==legacy,"missing-state legacy request changed");
        require(native_commerce_headers("https://example.invalid",{}) .empty(),"ordinary request changed");
        std::cout<<"commerce contract passed\n";
    } catch (const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; }
}
