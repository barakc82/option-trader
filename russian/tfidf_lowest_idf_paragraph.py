import math
import re
import sys
from collections import Counter
from pathlib import Path

INPUT_FILE = Path(__file__).resolve().parent / 'igra-prestolov.txt'
TOKEN_RE = re.compile(r'\w+', re.UNICODE)
MIN_WORD_COUNT = 20


def load_paragraphs(path):
    """Group consecutive non-blank lines into paragraphs, tracking each paragraph's starting line number (1-indexed)."""
    paragraphs = []
    current_lines = []
    start_line = None

    with open(path, encoding='utf-8') as f:
        for line_number, raw_line in enumerate(f, start=1):
            if raw_line.strip():
                if start_line is None:
                    start_line = line_number
                current_lines.append(raw_line.rstrip('\n'))
            elif current_lines:
                paragraphs.append((start_line, ' '.join(current_lines)))
                current_lines = []
                start_line = None

    if current_lines:
        paragraphs.append((start_line, ' '.join(current_lines)))

    return paragraphs


def tokenize(text):
    return [t.lower() for t in TOKEN_RE.findall(text)]


def build_tfidf(paragraphs):
    """Returns (tf, idf, tfidf): tf/tfidf are per-paragraph term dicts aligned with `paragraphs`, idf is term -> idf."""
    tokenized = [tokenize(text) for _, text in paragraphs]
    num_docs = len(paragraphs)

    document_frequency = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))

    idf = {term: math.log(num_docs / df) for term, df in document_frequency.items()}

    tf = [Counter(tokens) for tokens in tokenized]
    tfidf = [{term: count * idf[term] for term, count in doc_tf.items()} for doc_tf in tf]

    return tf, idf, tfidf


def find_lowest_overall_idf_paragraph(tf, idf):
    """Paragraph whose terms have the lowest average idf, i.e. built from the most common/generic vocabulary.
    Only paragraphs with more than MIN_WORD_COUNT words are considered."""
    best_index = None
    best_score = None

    for i, doc_tf in enumerate(tf):
        word_count = sum(doc_tf.values())
        if word_count <= MIN_WORD_COUNT:
            continue
        avg_idf = sum(idf[term] for term in doc_tf) / len(doc_tf)
        if best_score is None or avg_idf < best_score:
            best_score = avg_idf
            best_index = i

    return best_index, best_score


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    paragraphs = load_paragraphs(INPUT_FILE)
    tf, idf, tfidf = build_tfidf(paragraphs)

    best_index, best_score = find_lowest_overall_idf_paragraph(tf, idf)
    if best_index is None:
        print("No paragraph with tokenizable content was found.")
        return

    start_line, text = paragraphs[best_index]
    print(f"Lowest overall IDF: {best_score:.4f}")
    print(f"Paragraph starts at line {start_line}:")
    print(text)


if __name__ == '__main__':
    main()
