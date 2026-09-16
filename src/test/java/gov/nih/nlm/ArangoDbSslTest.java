package gov.nih.nlm;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Map;

import org.junit.jupiter.api.Test;

class ArangoDbSslTest {

	@Test
	void loopbackOrUnsetHostDefaultsToHttp() {
		assertFalse(ArangoDbUtilities.useSsl(Map.of()));
		assertFalse(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "")));
		for (String host : new String[] { "localhost", "127.0.0.1", "::1" }) {
			assertFalse(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", host)), host);
		}
	}

	@Test
	void remoteHostDefaultsToHttps() {
		assertTrue(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "10.0.1.5")));
	}

	@Test
	void explicitSchemeOverridesDefault() {
		assertFalse(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "10.0.1.5", "ARANGO_DB_SCHEME", "http")));
		assertTrue(ArangoDbUtilities.useSsl(Map.of("ARANGO_DB_HOST", "localhost", "ARANGO_DB_SCHEME", "HTTPS")));
	}
}
