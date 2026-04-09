package gov.nih.nlm;

import javax.xml.stream.XMLEventFactory;
import javax.xml.stream.XMLEventReader;
import javax.xml.stream.XMLEventWriter;
import javax.xml.stream.XMLInputFactory;
import javax.xml.stream.XMLOutputFactory;
import javax.xml.stream.XMLStreamException;
import javax.xml.stream.events.XMLEvent;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

import static gov.nih.nlm.PathUtilities.OBO_DIR;

/**
 * Filters a large OWL ontology file to retain only classes that have a taxon restriction
 * for a specified NCBITaxon. Uses StAX streaming to handle large files with minimal memory.
 */
public class OntologySlimmer {

    private static final String OWL_NS = "http://www.w3.org/2002/07/owl#";
    private static final String RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
    private static final String SOME_VALUES_FROM = "http://purl.obolibrary.org/obo/NCBITaxon_";

    /**
     * Filter an OWL file to retain only classes with a taxon restriction for the given NCBI taxon ID.
     * Copies the ontology header, all annotation and object property declarations, and only those
     * owl:Class elements that contain an owl:someValuesFrom restriction referencing the specified taxon.
     * Drops all owl:Axiom elements.
     *
     * @param inputFile  Path to the full OWL file
     * @param outputFile Path to write the filtered OWL file
     * @param taxonId    NCBI taxon ID to filter on (e.g., "9606" for human)
     * @return The number of classes written
     * @throws IOException        if an I/O error occurs
     * @throws XMLStreamException if an XML parsing error occurs
     */
    public static int slimOntology(Path inputFile, Path outputFile, String taxonId) throws IOException, XMLStreamException {
        String taxonURI = "http://purl.obolibrary.org/obo/NCBITaxon_" + taxonId;

        XMLInputFactory inputFactory = XMLInputFactory.newInstance();
        XMLOutputFactory outputFactory = XMLOutputFactory.newInstance();
        XMLEventFactory eventFactory = XMLEventFactory.newInstance();

        int classesWritten = 0;
        int classesSkipped = 0;

        try (var fis = new BufferedInputStream(new FileInputStream(inputFile.toFile()));
             var fos = new BufferedOutputStream(new FileOutputStream(outputFile.toFile()))) {

            XMLEventReader reader = inputFactory.createXMLEventReader(fis);
            XMLEventWriter writer = outputFactory.createXMLEventWriter(fos, "UTF-8");

            List<XMLEvent> buffer = new ArrayList<>();
            boolean inClass = false;
            boolean inAxiom = false;
            boolean hasTaxon = false;
            int depth = 0;
            int elementStartDepth = 0;

            while (reader.hasNext()) {
                XMLEvent event = reader.nextEvent();

                if (event.isStartElement()) {
                    depth++;
                    String localName = event.asStartElement().getName().getLocalPart();
                    String nsURI = event.asStartElement().getName().getNamespaceURI();

                    if (nsURI.equals(OWL_NS) && localName.equals("Class") && !inClass && !inAxiom) {
                        // Check if this is a top-level owl:Class (has rdf:about attribute)
                        var aboutAttr = event.asStartElement().getAttributeByName(
                                new javax.xml.namespace.QName(RDF_NS, "about"));
                        if (aboutAttr != null) {
                            inClass = true;
                            elementStartDepth = depth;
                            hasTaxon = false;
                            buffer.clear();
                            buffer.add(event);
                            continue;
                        }
                    }

                    if (nsURI.equals(OWL_NS) && localName.equals("Axiom") && !inClass) {
                        inAxiom = true;
                        elementStartDepth = depth;
                        continue;
                    }

                    if (inClass) {
                        // Check for owl:someValuesFrom with taxon URI
                        if (nsURI.equals(OWL_NS) && localName.equals("someValuesFrom")) {
                            var resourceAttr = event.asStartElement().getAttributeByName(
                                    new javax.xml.namespace.QName(RDF_NS, "resource"));
                            if (resourceAttr != null && resourceAttr.getValue().equals(taxonURI)) {
                                hasTaxon = true;
                            }
                        }
                        buffer.add(event);
                        continue;
                    }

                    if (inAxiom) {
                        continue;
                    }

                    writer.add(event);

                } else if (event.isEndElement()) {
                    if (inClass) {
                        buffer.add(event);
                        if (depth == elementStartDepth) {
                            inClass = false;
                            if (hasTaxon) {
                                for (XMLEvent e : buffer) {
                                    writer.add(e);
                                }
                                classesWritten++;
                            } else {
                                classesSkipped++;
                            }
                            buffer.clear();
                        }
                    } else if (inAxiom) {
                        if (depth == elementStartDepth) {
                            inAxiom = false;
                        }
                    } else {
                        writer.add(event);
                    }
                    depth--;

                } else {
                    // Characters, comments, processing instructions, etc.
                    if (inClass) {
                        buffer.add(event);
                    } else if (!inAxiom) {
                        writer.add(event);
                    }
                }
            }

            writer.flush();
            reader.close();
        }

        System.out.println("Wrote " + classesWritten + " classes, skipped " + classesSkipped
                + " classes and all axioms");
        return classesWritten;
    }

    /**
     * Filter pr.owl in data/obo to retain only human (NCBITaxon 9606) protein classes.
     * Reads pr.owl, writes the filtered result to pr-9606.owl, then replaces pr.owl.
     *
     * @param args (None expected)
     */
    public static void main(String[] args) {
        Path fullFile = OBO_DIR.resolve("pr.owl");
        Path slimFile = OBO_DIR.resolve("pr-9606.owl");
        Path backupFile = OBO_DIR.resolve("pr-full.owl");

        if (!Files.exists(fullFile)) {
            throw new RuntimeException("pr.owl not found in " + OBO_DIR);
        }

        try {
            System.out.println("Slimming " + fullFile + " to taxon 9606");
            long startTime = System.nanoTime();
            int classesWritten = slimOntology(fullFile, slimFile, "9606");
            long stopTime = System.nanoTime();
            System.out.println("Slimmed in " + (stopTime - startTime) / 1e9 + " s");

            // Rename pr.owl -> pr-full.owl, pr-9606.owl -> pr.owl
            System.out.println("Renaming " + fullFile + " to " + backupFile);
            Files.move(fullFile, backupFile);
            System.out.println("Renaming " + slimFile + " to " + fullFile);
            Files.move(slimFile, fullFile);

        } catch (IOException | XMLStreamException e) {
            throw new RuntimeException(e);
        }
    }
}
