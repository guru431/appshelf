// Offline account-file contract: concurrent writers, replacement failure and cleanup.
#include "atomic_account.h"
#include <atomic>
#include <fstream>
#include <iostream>
#include <thread>
#include <vector>
#ifndef _WIN32
#include <sys/wait.h>
#endif

static void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}

static std::string read(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

int main(int argc, char** argv) {
    try {
        require(argc == 2, "contract needs a disposable directory");
        const std::filesystem::path root = argv[1], account = root / "account";
        std::filesystem::create_directories(root);
        const std::vector<unsigned char> initial(65536, 'a');
        require(write_account_file(account, initial), "initial account write failed");
        std::atomic<bool> start{false}, ok{true};
        std::vector<std::thread> writers;
        for (int i = 0; i < 4; ++i) {
            writers.emplace_back([&, i] {
                const std::vector<unsigned char> blob(65536, 'b' + i);
                while (!start.load()) std::this_thread::yield();
                for (int n = 0; n < 8; ++n)
                    if (!write_account_file(account, blob)) ok = false;
            });
        }
        start = true;
        for (auto& writer : writers) writer.join();
        require(ok, "concurrent account writer failed");
        auto saved = read(account);
        require(saved.size() == initial.size() && saved.find_first_not_of(saved[0]) == std::string::npos,
                "concurrent writers corrupted account");
#ifndef _WIN32
        // A different process must also get its own temporary file.
        std::vector<pid_t> children;
        for (int i = 0; i < 4; ++i) {
            const auto pid = fork();
            require(pid >= 0, "fork failed");
            if (pid == 0) {
                for (int n = 0; n < 8; ++n)
                    if (!write_account_file(account, std::vector<unsigned char>(65536, 'f' + i))) _exit(1);
                _exit(0);
            }
            children.push_back(pid);
        }
        for (const auto pid : children) {
            int status = 0;
            require(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0,
                    "concurrent account process failed");
        }
        saved = read(account);
        require(saved.size() == initial.size() && saved.find_first_not_of(saved[0]) == std::string::npos,
                "concurrent processes corrupted account");
#else
        const auto locked = CreateFileW(account.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                                        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        require(locked != INVALID_HANDLE_VALUE, "cannot lock account for replacement failure");
        const bool replaced = write_account_file(account, initial);
        CloseHandle(locked);
        require(!replaced && read(account) == saved, "failed replacement changed account");
#endif
        // A failed rename must preserve its target and remove only this writer's temporary file.
        const auto directory = root / "directory";
        std::filesystem::create_directory(directory);
        require(!write_account_file(directory, initial) && std::filesystem::is_directory(directory),
                "failed rename replaced destination");
        for (const auto& entry : std::filesystem::directory_iterator(root))
            require(entry.path() == account || entry.path() == directory, "temporary account file leaked");
        require(!write_account_file(root / "missing" / "account", initial), "missing parent accepted");
        std::cout << "account contract passed\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n'; return 1;
    }
}
