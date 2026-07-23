import json
import os
from pathlib import Path
import re
from time import sleep
from urllib import parse

import requests
from lxml import etree

EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
NCBI_EMAIL = os.environ.get("NCBI_EMAIL")
NCBI_API_KEY = os.environ.get("NCBI_API_KEY")
NCBI_API_SLEEP = 0.2
REQUEST_TIMEOUT = 30  # seconds


def parse_xml(xml_data):
    """Parse XML text or bytes into an lxml element.

    Encodes ``str`` input to bytes so lxml honors any XML encoding
    declaration (e.g. ``<?xml ... encoding="UTF-8"?>``), which lxml
    rejects when handed a ``str``.

    Parameters
    ----------
    xml_data : str or bytes
        Raw XML response text

    Returns
    -------
    lxml.etree._Element
        The root element of the parsed document
    """
    if isinstance(xml_data, str):
        xml_data = xml_data.encode("utf-8")
    return etree.fromstring(xml_data)


def element_text(element):
    """Return the concatenated text of an element and its descendants.

    Mirrors BeautifulSoup's ``Tag.text``: lxml's ``Element.text`` yields
    only the leading text node, so descendant and tail text (e.g. inline
    markup inside a title) must be joined explicitly.

    Parameters
    ----------
    element : lxml.etree._Element
        Any parsed element

    Returns
    -------
    str
        All descendant text in document order
    """
    return "".join(element.itertext())


def extract_uniprot_name(xml_data):
    """Extract the UniProt accession from raw Entrezgene XML.

    Scans all ``<Other-source_url>`` elements for a uniprot.org URL and
    returns the accession from the last match in document order. For
    genes with a single UniProt cross-reference this is unambiguous; for
    isoformic genes with multiple cross-references, the choice is
    arbitrary-but-stable and matches the historical behavior of the
    deprecated fetcher and ``parse_xml_for_gene_id``.

    Parameters
    ----------
    xml_data : str
        Raw Entrezgene XML response text

    Returns
    -------
    str or None
        UniProt accession, or None if no uniprot.org URL is present
    """
    link = None
    for child in parse_xml(xml_data).iter("Other-source_url"):
        text = element_text(child)
        if "www.uniprot.org" in text:
            link = text
    if link is None:
        return None
    return Path(parse.urlparse(link).path).stem


def find_names_or_none(root, names, attribute=None):
    """Find the text, or specified attribute, in the last named tag,
    if all previously named tags are found.

    Parameters
    ----------
    root : lxml.etree._Element
        Any parsed element
    names : list(str)
        List of tag names to find in order
    attribute : str
        Attribute of the last named tag

    Returns
    -------
    str
        text, or attribute, in the last named tag, or None
    """
    # lxml elements are falsy when they have no children, so identity
    # checks against None are required rather than truthiness tests. Each
    # name is searched on the descendant axis (".//") to match the
    # recursive lookup BeautifulSoup's Tag.find performs.
    element = root.find(f".//{names[0]}")
    for name in names[1:]:
        if element is not None:
            element = element.find(f".//{name}")
    if element is not None:
        if attribute:
            return element.get(attribute)
        else:
            return element_text(element)
    else:
        return None


def get_data_for_pmid(pmid, do_write=False):
    """Fetch from PubMed using a PMID to find the last name of the
    first author, journal title, article title, and article year of
    publication.

    Parameters
    ----------
    pmid : str
        The PubMed identifier to use in the fetch
    do_write : bool
        Flag to write fetched results, or not (default: False)

    Returns
    -------
    data : dict
       Dictionary containing the last name of the first author,
       journal title, article title, and article year of publication
    """
    # Need a default return value
    data = {}

    # Fetch from PubMed
    print(f"Getting data for PMID: '{pmid}'")
    fetch_url = EUTILS_URL + "efetch.fcgi"
    params = {
        "db": "pubmed",
        "id": pmid,
        "rettype": "xml",
        "email": NCBI_EMAIL,
        "api_key": NCBI_API_KEY,
    }
    sleep(NCBI_API_SLEEP)
    response = requests.get(
        fetch_url, params=parse.urlencode(params, safe=","), timeout=REQUEST_TIMEOUT
    )
    if response.status_code == 200:
        xml_data = response.text
        if do_write:
            with open(f"{pmid}.xml", "w") as fp:
                fp.write(
                    etree.tostring(parse_xml(xml_data), pretty_print=True).decode(
                        "utf-8"
                    )
                )

        # Got the page, so parse it, and search for the title
        root = parse_xml(xml_data).find(".//Article")
        if root is not None:
            data["Author"] = find_names_or_none(
                root, ["AuthorList", "Author", "LastName"]
            )  # First author
            if len(find_names_or_none(root, ["AuthorList"])) > 1:
                data["Author"] += " et al."
            data["Journal"] = find_names_or_none(root, ["Journal", "ISOAbbreviation"])
            data["Title"] = find_names_or_none(root, ["ArticleTitle"])
            data["Year"] = find_names_or_none(root, ["ArticleDate", "Year"])
            data["Citation"] = f"{data['Author']} ({data['Year']}) {data['Journal']}"
    else:
        print(f"Encountered error in fetching from PubMed: {response.status_code}")

    return data


def find_gene_id_for_gene_name(name, do_write=False):
    """Search Gene using a gene name to find the corresponding gene
    id.

    Parameters
    ----------
    name : str
       The gene name for which to search
    do_write : bool
        Flag to write fetched results, or not (default: False)

    Returns
    -------
    str
       The gene id
    """
    # Need a default return value
    gene_id = None

    # Search Gene
    print(f"Searching Gene for name: '{name}'")
    search_url = EUTILS_URL + "esearch.fcgi"
    params = {
        "db": "gene",
        "term": f"{name}[Gene Name] AND 9606[Taxonomy ID]",
        "sort": "relevance",
        "retmax": 1,
        "retmode": "json",
        "email": NCBI_EMAIL,
        "api_key": NCBI_API_KEY,
    }
    sleep(NCBI_API_SLEEP)
    response = requests.get(
        search_url, params=parse.urlencode(params, safe=","), timeout=REQUEST_TIMEOUT
    )
    if response.status_code == 200:
        json_data = response.json()
        if do_write:
            with open(f"{name}.json", "w") as fp:
                json.dump(json_data, fp, indent=4)

        # Got the response, so assign the gene id
        if len(json_data["esearchresult"]["idlist"]) > 0:
            gene_id = json_data["esearchresult"]["idlist"][0]
            print(f"Found gene id {gene_id} while searching Gene for name {name}")

        else:
            print(f"No gene id found while searching Gene for name {name}")

    else:
        print(
            f"Encountered error in searching Gene for name {name}: {response.status_code}"
        )

    return gene_id


def fetch_xml_for_gene_id(gene_id):
    """Fetch raw XML from Gene using a gene id.

    Parameters
    ----------
    gene_id : str
        The Gene identifier to use in the fetch

    Returns
    -------
    xml_data : str or None
        Raw XML response text, or None if the request failed
    """
    print(f"Fetching XML for gene id: '{gene_id}'")
    fetch_url = EUTILS_URL + "efetch.fcgi"
    params = {
        "db": "gene",
        "id": gene_id,
        "retmode": "xml",
        "email": NCBI_EMAIL,
        "api_key": NCBI_API_KEY,
    }
    response = requests.get(
        fetch_url, params=parse.urlencode(params, safe=","), timeout=REQUEST_TIMEOUT
    )
    if response.status_code == 200:
        return response.text
    else:
        print(f"Encountered error in fetching from Gene: {response.status_code}")
        return None


def parse_xml_for_gene_id(gene_id, xml_data):
    """Parse raw Gene XML and extract required values.

    Parameters
    ----------
    gene_id : str
        The Gene identifier
    xml_data : str
        Raw XML response text from NCBI Gene

    Returns
    -------
    data : dict
        Dictionary containing the required values of the full record
    """
    data = {}

    tags = parse_xml(xml_data).xpath("//Entrezgene")
    if len(tags) == 0:
        raise Exception(f"No Entrezgene element found for gene_id={gene_id!r}")
    if len(tags) > 1:
        raise Exception("Expect a single Entrezgene element")
    root = tags[0]
    data["Gene_ID"] = gene_id
    data["Official_symbol"] = find_names_or_none(
        root,
        [
            "Entrezgene_gene",
            "Gene-ref",
            "Gene-ref_formal-name",
            "Gene-nomenclature_symbol",
        ],
    )
    data["Official_full_name"] = find_names_or_none(
        root,
        [
            "Entrezgene_gene",
            "Gene-ref",
            "Gene-ref_formal-name",
            "Gene-nomenclature_name",
        ],
    )
    data["Gene_type"] = find_names_or_none(root, ["Entrezgene_type"], attribute="value")
    data["Link_to_UniProt_ID"] = None
    for child in root.iter("Other-source_url"):
        text = element_text(child)
        if "www.uniprot.org" in text:
            data["Link_to_UniProt_ID"] = text
    data["Organism"] = find_names_or_none(
        root,
        [
            "Entrezgene_source",
            "BioSource",
            "BioSource_org",
            "Org-ref",
            "Org-ref_taxname",
        ],
    )
    data["RefSeq_gene_ID"] = None
    for child in root.iter("Gene-commentary_heading"):
        text = element_text(child)
        if "GCF_" in text:
            m = re.search(r":\s*(GCF_.*)", text)
            if m:
                data["RefSeq_gene_ID"] = m.group(1)
    data["Also_known_as"] = []
    for child in root.iter("Gene-ref_syn_E"):
        data["Also_known_as"].append(element_text(child))
    data["Summary"] = find_names_or_none(root, ["Entrezgene_summary"])
    pr_desc = find_names_or_none(root, ["Entrezgene_prot", "Prot-ref_desc"])
    # Derive the UniProt accession from the link already extracted above
    # rather than re-parsing the XML a second time. extract_uniprot_name
    # scans the same <Other-source_url> elements and returns the accession
    # from the last uniprot.org match, which is exactly what
    # Link_to_UniProt_ID holds here.
    if data["Link_to_UniProt_ID"] is not None:
        data["UniProt_name"] = Path(
            parse.urlparse(data["Link_to_UniProt_ID"]).path
        ).stem
    else:
        data["UniProt_name"] = None
    for product in root.iter("Gene-commentary_products"):
        if find_names_or_none(product, ["Gene-commentary_type"], "value") == "mRNA":
            nm_id = None
            np_id = None
            for accession in product.iter("Gene-commentary_accession"):
                text = element_text(accession)
                if "NM_" in text:
                    nm_id = text
                elif "NP_" in text:
                    np_id = text
            if nm_id and np_id and pr_desc:
                data["mRNA_(NM)_and_protein_(NP)_sequences"] = (
                    f"{nm_id} -> {np_id}, {pr_desc}"
                )
            break

    return data


def get_data_for_gene_id(gene_id, do_write=False):
    """Fetch from Gene using a gene id to get the full record and find
    required values.

    Parameters
    ----------
    gene_id : str
        The Gene identifier to use in the fetch
    do_write : bool
        Flag to write fetched results, or not (default: False)

    Returns
    -------
    data : dict
       Dictionary containing the required values of the full record
    """
    # Need a default return value
    data = {}

    xml_data = fetch_xml_for_gene_id(gene_id)
    if xml_data is not None:
        if do_write:
            with open(f"{gene_id}.xml", "w") as fp:
                fp.write(
                    etree.tostring(parse_xml(xml_data), pretty_print=True).decode(
                        "utf-8"
                    )
                )

        data = parse_xml_for_gene_id(gene_id, xml_data)

    return data


def main():
    print(get_data_for_pmid("37291214"))
    print(get_data_for_gene_id("1080"))


if __name__ == "__main__":
    import JsonErrors

    JsonErrors.install()
    main()
