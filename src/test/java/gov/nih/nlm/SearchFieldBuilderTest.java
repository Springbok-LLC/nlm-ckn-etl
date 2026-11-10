package gov.nih.nlm;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class SearchFieldBuilderTest {

    @Test
    void build_oboCollection() {
        assertEquals(List.of("UBERON:0002405",
                        "UBERON_0002405",
                        "http://purl.obolibrary.org/obo/UBERON_0002405",
                        "https://purl.obolibrary.org/obo/UBERON_0002405"),
                SearchFieldBuilder.build("UBERON", "0002405"));
    }

    @Test
    void build_oboCollectionKeepsMixedCase() {
        assertEquals(List.of("NCBITaxon:9606",
                        "NCBITaxon_9606",
                        "http://purl.obolibrary.org/obo/NCBITaxon_9606",
                        "https://purl.obolibrary.org/obo/NCBITaxon_9606"),
                SearchFieldBuilder.build("NCBITaxon", "9606"));
    }

    @Test
    void build_nonOboCollectionUsesKeyForms() {
        assertEquals(List.of("CS:abc123", "CS_abc123"), SearchFieldBuilder.build("CS", "abc123"));
    }

    @Test
    void build_orphanetIsNotObo() {
        assertEquals(List.of("Orphanet:558", "Orphanet_558"), SearchFieldBuilder.build("Orphanet", "558"));
    }

    @Test
    void build_keyWithUnderscoresIsKeptWhole() {
        assertEquals(List.of("CSD:dv1__lung", "CSD_dv1__lung"), SearchFieldBuilder.build("CSD", "dv1__lung"));
    }
}
