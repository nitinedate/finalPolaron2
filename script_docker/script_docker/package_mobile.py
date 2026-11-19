from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
root=Path('/mnt/data/mobile_perf_work')
out=Path('/mnt/data')
fixes=['backend/app/services/forensic_serial_mobile.py','backend/tests/test_mobile_parse_batch_claim_20261111.py','MOBILE_NATIVE_PARSE_PERFORMANCE_FIX_20261111.md']
patch=out/'Polaron_Mobile_Native_Parsing_Performance_Patch_20261111.zip'
with ZipFile(patch,'w',ZIP_DEFLATED,compresslevel=7) as z:
 for f in fixes: z.write(root/f,f)
full=out/'Polaron_Complete_Mobile_Native_Parsing_Optimized_20261111.zip'
original=out/'Polaron_Complete_Inventory_Errno5_Fixed_20261111.zip'
with ZipFile(original) as src,ZipFile(full,'w',ZIP_DEFLATED,compresslevel=6) as z:
  original_names=set(src.namelist())
  for i in src.infolist():
   if i.filename in fixes: continue
   z.writestr(i,src.read(i.filename))
  for f in fixes: z.write(root/f,f)
for p in (patch,full):
 with ZipFile(p) as z:
  print(p, p.stat().st_size,'files',len(z.namelist()),'bad',z.testzip())
