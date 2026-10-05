package gov.nih.nlm;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Map;

import org.junit.jupiter.api.Test;

class ArangoDbSslTest {

	@Test
	void loopbackOrUnsetHostDefaultsToHttp() {
		assertFalse(ArangoDbUtilities.useSsl(Map.of()));
		assertFalse(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "")));
		for (String host : new String[] { "localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "::1",
				"0:0:0:0:0:0:0:1", "::ffff:127.0.0.1" }) {
			assertFalse(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", host)), host);
		}
	}

	@Test
	void remoteHostDefaultsToHttps() {
		for (String host : new String[] { "10.0.1.5", "::2", "localhost.example.com", "127.0.0.1.example.com",
				"arangodb" }) {
			assertTrue(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", host)), host);
		}
	}

	@Test
	void explicitSchemeOverridesDefault() {
		assertFalse(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "10.0.1.5", "ARANGO_DB_SCHEME", "http")));
		assertTrue(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "localhost", "ARANGO_DB_SCHEME", "HTTPS")));
		assertFalse(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "10.0.1.5", "ARANGO_DB_SCHEME", " http ")));
	}

	@Test
	void invalidSchemeIsRejected() {
		for (String scheme : new String[] { "tcp", "htps", "https://" }) {
			assertThrows(IllegalArgumentException.class,
					() -> ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "10.0.1.5", "ARANGO_DB_SCHEME", scheme)),
					scheme);
		}
	}

	@Test
	void blankSchemeFallsBackToHostDefault() {
		assertFalse(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "localhost", "ARANGO_DB_SCHEME", "   ")));
		assertTrue(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "10.0.1.5", "ARANGO_DB_SCHEME", "   ")));
	}
}
