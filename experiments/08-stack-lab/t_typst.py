import sys, typst, pypandoc
src = pypandoc.convert_file(sys.argv[1], "typst", format="gfm")
open("out/sample.typ","w",encoding="utf-8").write('#set text(lang: "ru")\n' + src)
typst.compile("out/sample.typ", output=sys.argv[2], ignore_system_fonts=True)
print("fonts:", typst.Fonts(include_system_fonts=False, include_embedded_fonts=True).families()[:20] if hasattr(typst,"Fonts") else "n/a")
