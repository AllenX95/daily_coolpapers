import unittest

from daily_coolpapers.crawler import parse_papers


class CrawlerMetadataTests(unittest.TestCase):
    def test_nested_metadata_links_do_not_leak_into_abstract(self):
        html = """
        <h2>
          <a href="/arxiv/2608.12345">#1</a>
          <a href="/arxiv/2608.12345">Structured metadata</a>
        </h2>
        <p>Authors:<a>Ada Lovelace</a><a>Alan Turing</a></p>
        <p>A clean abstract sentence.</p>
        <p>Subjects:<a>cs.AI</a><a>cs.LG</a></p>
        <p>Publish: 2026-08-09</p>
        <nav><a href="/calendar">Calendar</a> Footer controls</nav>
        """

        paper = parse_papers(html, "cs.AI", 30, "https://papers.cool/arxiv/cs.AI")[0]

        self.assertEqual(paper.authors, ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(paper.subjects, ["cs.AI", "cs.LG"])
        self.assertEqual(paper.abstract, "A clean abstract sentence.")

    def test_metadata_label_variants_keep_section_ownership(self):
        html = """
        <h2>
          <a href="/arxiv/2608.12346">#1</a>
          <a href="/arxiv/2608.12346">Variant labels</a>
        </h2>
        <div>
          <p>Author(s)：<span>Grace Hopper</span><span>Edsger Dijkstra</span></p>
          <p>Abstract：A second clean abstract.</p>
          <p>Categories：<span>cs.SE</span><span>cs.AI</span></p>
          <p>Published：2026-08-08</p>
        </div>
        """

        paper = parse_papers(html, "cs.SE", 30, "https://papers.cool/arxiv/cs.SE")[0]

        self.assertEqual(paper.authors, ["Grace Hopper", "Edsger Dijkstra"])
        self.assertEqual(paper.subjects, ["cs.SE", "cs.AI"])
        self.assertEqual(paper.abstract, "A second clean abstract.")
        self.assertEqual(paper.published_at, "2026-08-08")

    def test_label_links_are_not_returned_as_metadata_values(self):
        html = """
        <h2><a href="/arxiv/2608.12347">#1</a><a href="/arxiv/2608.12347">Links</a></h2>
        <p><a>Authors:</a><a>Barbara Liskov</a><a>Donald Knuth</a></p>
        <p>Compiler and language research.</p>
        <p><a>Subjects:</a><a>cs.PL</a><a>cs.SE</a></p>
        <p>Publish: 2026-08-07</p>
        """

        paper = parse_papers(html, "cs.PL", 30, "https://papers.cool/arxiv/cs.PL")[0]

        self.assertEqual(paper.authors, ["Barbara Liskov", "Donald Knuth"])
        self.assertEqual(paper.subjects, ["cs.PL", "cs.SE"])
        self.assertEqual(paper.abstract, "Compiler and language research.")


if __name__ == "__main__":
    unittest.main()
