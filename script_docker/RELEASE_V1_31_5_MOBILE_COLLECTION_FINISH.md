# Polaron V1.31.5: a finished iPhone collection no longer looks stuck at 95%

## What was reported
An iPhone collection made on the examiner's computer (NITIN), with the forensic server elsewhere (NITIN-OFFICE-1):
- the page showed **95%, Stage: acquire** for 30+ minutes;
- the case folder already held the ZIP, PAS, UFD and UFDX packages (21:48);
- the helper's job result said `"status": "completed"`, `"stage_reached": "complete"`, and verification passed for
  191,689 files (160.5 GB).

## What was actually going on
1. **The collection was finished.** With the forensic server on another computer, one step remains: the server
   copies the image into its own case folder (step 2 under "Forensic server copy"). That copy failed at once:
   "The Client Evidence Agent is not running on this computer". The page then kept the last in-flight numbers
   (95%, acquire) on screen instead of saying the collection was done and only the copy was left.
2. **Why 95% in particular.** The evidence ZIP holds the backup (`02_Original_Extraction`) and the readable copies
   (WhatsApp, messages, photos). Both count towards its size, but only the backup part reported progress. The bar
   therefore stopped where the backup part ended (about 95%) while the readable copies were written, and the page
   labelled that packaging work as "acquire".

## What changes
- **Finished means finished.** When the collection is sealed on this computer and only the server copy is left, the
  page shows 100%, "Complete", and "Image created on this computer. What is left: copy it to the forensic server
  for processing". The phone agent box reads "finished on this computer".
- **Packaging reports progress to the end.** The readable copies part of the ZIP reports progress too, continuing
  from where the backup part stopped. Examples: "Packaging the evidence ZIP - readable copies (WhatsApp, messages,
  photos): 150.2 of 160.5 GB". The page labels it Sealing, at 90-99.5%, and never shows 100% before it is done.
- **When the agent is missing**, the error box also offers **Copy through this browser instead (no install)**. The
  case folder is chosen in this browser, which serves it to the server; keep the tab open. For images this size, the
  agent is the better choice: it is faster and keeps copying when the tab is closed.

## What you do for the collection on screen now (nothing is lost)
On NITIN:
1. Install the Client Evidence Agent once. Either follow the yellow box: download the two files, run
   `Install-PolaronClientEvidenceAgent.cmd`, approve the administrator prompt, enter
   `https://forensic.future-softtech.co.in`. Or paste the PowerShell line shown there.
2. Click **Check again**.
3. Choose what to copy:
   - **Evidence packages, logs and hashes** copies about 160 GB (`05_Exports` with the full ZIP, reports, logs,
     hashes). The ZIP contains the whole collection (`full_payload_in_zip: true`) and is what the server processes.
   - **Full** also copies the unpacked `02_Original_Extraction` and `03_Working_Copy`, roughly the same data again.
4. Click **Copy to the forensic server again**. The server checks every file with SHA-256, then processing starts on
   the server.

On the server, deploy as usual (`start_docker_prod.cmd`, then Ctrl+F5) so the page shows the finished state.

## Tests
- `backend/tests/test_mobile_collection_finish_v1_31_5.py`: both ZIP parts report progress, the readable part
  continues from the backup part and reaches 99% or more, and the ZIP contents are complete.
- Export-package suites: 53 passed (49 before), no failures.
- Frontend: `tsc` clean, vitest 66/66, `vite build` OK.
