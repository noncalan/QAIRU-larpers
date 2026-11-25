import json, typst, datetime
rep = {"doc": "requirements.md", "model": "claude-opus-5-5", "version": "v1.0-2-gabc123",
 "findings": [{"id":"D-01","loc":"Ф-2, стр. 38","quote":"Месяц определяется по дате совершения покупки в Алматы. #hash $dollar *star* _u_ @at <lt> [br] \\ `bt`","sev":"Критично"},
              {"id":"D-02","loc":"Часть B, стр. 104","quote":"REST type | GET; 1 000–1 000 000 ₸; Қазақстан","sev":"Критично"}]}
ts = datetime.datetime(2026,11,27,12,0,tzinfo=datetime.timezone.utc)
for i in (1,2):
    pdf, warns = typst.Compiler().compile_with_warnings(input="t_report.typ", output=f"out/report{i}.pdf", sys_inputs={"report": json.dumps(rep, ensure_ascii=False)}, timestamp=ts) if hasattr(typst,"Compiler") else (None,[])
    print("warnings:", [str(w) for w in warns])
import hashlib
print([hashlib.sha256(open(f"out/report{i}.pdf","rb").read()).hexdigest()[:12] for i in (1,2)])
