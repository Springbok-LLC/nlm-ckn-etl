package gov.nih.nlm;

import gov.nih.nlm.AqlQuerySetBuilder.AqlQuerySet;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * @deprecated Tests for deprecated {@link AqlQuerySetBuilder}.
 */
@Deprecated
@SuppressWarnings("deprecation")
class AqlQuerySetBuilderTest {

    @Test
    void getQuerySetInOne_bindVarsAndQuery() {
        AqlQuerySet qs = AqlQuerySetBuilder.getQuerySetInOne("testGraph", "BGS");

        assertEquals("testGraph", qs.bindVars().get("graph"));
        assertEquals("BGS", qs.bindVars().get("node"));
        assertEquals(2, qs.bindVars().size());
        assertTrue(qs.queryStr().contains("IN 1 ANY"));
    }

    @Test
    void getQuerySetInTwo_bindVarsAndQuery() {
        AqlQuerySet qs = AqlQuerySetBuilder.getQuerySetInTwo("testGraph", "CL", "GO");

        assertEquals("testGraph", qs.bindVars().get("graph"));
        assertEquals("CL", qs.bindVars().get("nodeOne"));
        assertEquals("GO", qs.bindVars().get("nodeTwo"));
        assertEquals(3, qs.bindVars().size());
        assertTrue(qs.queryStr().contains("IN 2 ANY"));
    }

    @Test
    void getQuerySetInTwoWithHierarchy_bindVarsAndQuery() {
        AqlQuerySet qs = AqlQuerySetBuilder.getQuerySetInTwoWithHierarchy(
                "testGraph", "CL", "NCBITaxon", "NCBITaxon-NCBITaxon", "SUB_CLASS_OF");

        assertEquals("testGraph", qs.bindVars().get("graph"));
        assertEquals("CL", qs.bindVars().get("nodeOne"));
        assertEquals("NCBITaxon", qs.bindVars().get("nodeTwo"));
        assertEquals("NCBITaxon-NCBITaxon", qs.bindVars().get("edgeCollection"));
        assertEquals("SUB_CLASS_OF", qs.bindVars().get("edgeLabel"));
        assertEquals(5, qs.bindVars().size());
        assertTrue(qs.queryStr().contains("OUTBOUND"));
        assertTrue(qs.queryStr().contains("IN 2 ANY"));
    }

    @Test
    void getQuerySetInThree_bindVarsAndQuery() {
        AqlQuerySet qs = AqlQuerySetBuilder.getQuerySetInThree("testGraph", "CL", "GS", "PR");

        assertEquals("testGraph", qs.bindVars().get("graph"));
        assertEquals("CL", qs.bindVars().get("nodeOne"));
        assertEquals("GS", qs.bindVars().get("nodeTwo"));
        assertEquals("PR", qs.bindVars().get("nodeThree"));
        assertEquals(4, qs.bindVars().size());
        assertTrue(qs.queryStr().contains("IN 3 ANY"));
    }

    @Test
    void getQuerySetInThreeWithHierarchy_bindVarsAndQuery() {
        AqlQuerySet qs = AqlQuerySetBuilder.getQuerySetInThreeWithHierarchy(
                "testGraph", "CL", "GS", "MONDO", "MONDO-MONDO", "SUB_CLASS_OF");

        assertEquals("testGraph", qs.bindVars().get("graph"));
        assertEquals("CL", qs.bindVars().get("nodeOne"));
        assertEquals("GS", qs.bindVars().get("nodeTwo"));
        assertEquals("MONDO", qs.bindVars().get("nodeThree"));
        assertEquals("MONDO-MONDO", qs.bindVars().get("edgeCollection"));
        assertEquals("SUB_CLASS_OF", qs.bindVars().get("edgeLabel"));
        assertEquals(6, qs.bindVars().size());
        assertTrue(qs.queryStr().contains("IN 3 ANY"));
        assertTrue(qs.queryStr().contains("OUTBOUND"));
    }

    @Test
    void getQuerySetInFour_bindVarsAndQuery() {
        AqlQuerySet qs = AqlQuerySetBuilder.getQuerySetInFour(
                "testGraph", "CL", "GS", "MONDO", "NCBITaxon");

        assertEquals("testGraph", qs.bindVars().get("graph"));
        assertEquals("CL", qs.bindVars().get("nodeOne"));
        assertEquals("GS", qs.bindVars().get("nodeTwo"));
        assertEquals("MONDO", qs.bindVars().get("nodeThree"));
        assertEquals("NCBITaxon", qs.bindVars().get("nodeFour"));
        assertEquals(5, qs.bindVars().size());
        assertTrue(qs.queryStr().contains("IN 4 ANY"));
    }

    @Test
    void getQuerySetInFourWithHierarchy_bindVarsAndQuery() {
        AqlQuerySet qs = AqlQuerySetBuilder.getQuerySetInFourWithHierarchy(
                "testGraph", "CL", "GS", "MONDO", "HP", "HP-HP", "SUB_CLASS_OF");

        assertEquals("testGraph", qs.bindVars().get("graph"));
        assertEquals("CL", qs.bindVars().get("nodeOne"));
        assertEquals("GS", qs.bindVars().get("nodeTwo"));
        assertEquals("MONDO", qs.bindVars().get("nodeThree"));
        assertEquals("HP", qs.bindVars().get("nodeFour"));
        assertEquals("HP-HP", qs.bindVars().get("edgeCollection"));
        assertEquals("SUB_CLASS_OF", qs.bindVars().get("edgeLabel"));
        assertEquals(7, qs.bindVars().size());
        assertTrue(qs.queryStr().contains("IN 4 ANY"));
        assertTrue(qs.queryStr().contains("OUTBOUND"));
    }

    @Test
    void getQuerySetInFive_bindVarsAndQuery() {
        AqlQuerySet qs = AqlQuerySetBuilder.getQuerySetInFive(
                "testGraph", "CL", "GS", "RS", "CHEMBL", "PR");

        assertEquals("testGraph", qs.bindVars().get("graph"));
        assertEquals("CL", qs.bindVars().get("nodeOne"));
        assertEquals("GS", qs.bindVars().get("nodeTwo"));
        assertEquals("RS", qs.bindVars().get("nodeThree"));
        assertEquals("CHEMBL", qs.bindVars().get("nodeFour"));
        assertEquals("PR", qs.bindVars().get("nodeFive"));
        assertEquals(6, qs.bindVars().size());
        assertTrue(qs.queryStr().contains("IN 5 ANY"));
    }
}
