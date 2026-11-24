package gov.nih.nlm;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Main code logs through {@link Events}; a {@code System.out} or {@code System.err}
 * call puts a line on the console that is not JSON.
 */
class NoConsoleOutputTest {

    private static final Path MAIN = Path.of("src/main/java");
    // System.out, or a static import that lets the code write ``out.println``.
    private static final Pattern CONSOLE = Pattern.compile(
            "\\bSystem\\s*\\.\\s*(out|err)\\b"
            + "|\\bimport\\s+static\\s+java\\s*\\.\\s*lang\\s*\\.\\s*System\\s*\\.\\s*(out|err|\\*)\\s*;");

    @Test
    void mainCodeDoesNotWriteToTheConsole() throws IOException {
        List<String> offenders;
        try (Stream<Path> files = Files.walk(MAIN)) {
            List<Path> sources = files.filter(p -> p.toString().endsWith(".java")).collect(Collectors.toList());
            assertFalse(sources.isEmpty(), "found no sources under " + MAIN);
            offenders = sources.stream()
                    .filter(p -> CONSOLE.matcher(read(p)).find())
                    .map(Path::toString)
                    .sorted()
                    .collect(Collectors.toList());
        }
        assertEquals(List.of(), offenders, "log with Events instead of System.out/System.err");
    }

    @Test
    void thePatternMatchesTheCallsItShouldCatch() {
        assertTrue(CONSOLE.matcher("System.out.println(x);").find());
        assertTrue(CONSOLE.matcher("System . err.print(x);").find());
        assertFalse(CONSOLE.matcher("System.getProperty(\"out\")").find());
        assertTrue(CONSOLE.matcher("import static java.lang.System.out;").find());
        assertTrue(CONSOLE.matcher("import static java.lang.System.err;").find());
        assertTrue(CONSOLE.matcher("import static java.lang.System.*;").find());
        assertFalse(CONSOLE.matcher("import static java.lang.Math.max;").find());
    }

    private static String read(Path p) {
        try {
            return Files.readString(p);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }
}
