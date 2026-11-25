import sys, markdown_it
from fpdf import FPDF
md = open(sys.argv[1], encoding="utf-8").read()
html = markdown_it.MarkdownIt("commonmark").enable("table").render(md)
pdf = FPDF(); pdf.add_page()
font = sys.argv[2]
pdf.add_font("Body", "", font); pdf.add_font("Body", "B", font); pdf.add_font("Body", "I", font); pdf.add_font("Body","BI",font)
pdf.set_font("Body", size=11)
pdf.write_html(html, font_family="Body")
pdf.output(sys.argv[3])
