import java.io.InputStream;
import java.nio.file.Path;
import java.security.CodeSigner;
import java.security.MessageDigest;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;

// JarFile verifies the signature block, signed manifest and entry digest on a full read.
class VerifyIndex {
    public static void main(String[] args) {
        try (JarFile jar = new JarFile(Path.of(args[0]).toFile(), true)) {
            JarEntry entry = jar.getJarEntry(args[2]);
            if (entry == null || entry.getSize() < 0 || entry.getSize() > 201326592L)
                throw new SecurityException();
            try (InputStream input = jar.getInputStream(entry)) {
                byte[] buf = new byte[65536];
                long total = 0;
                for (int n; (n = input.read(buf)) != -1;) {
                    total += n;
                    if (total > 201326592L) throw new SecurityException();
                }
            }
            CodeSigner[] signers = entry.getCodeSigners();
            if (signers == null || signers.length != 1) throw new SecurityException();
            byte[] cert = signers[0].getSignerCertPath().getCertificates().get(0).getEncoded();
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(cert);
            StringBuilder hex = new StringBuilder();
            for (byte b : digest) hex.append(String.format("%02x", b & 255));
            if (!hex.toString().equals(args[1])) throw new SecurityException();
            System.out.println("verified");
        } catch (Exception e) {
            System.exit(1);
        }
    }
}
