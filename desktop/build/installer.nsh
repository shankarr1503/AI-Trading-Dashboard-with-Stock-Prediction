; AI Trading Bot: NSIS additions (electron-builder "nsis.include").
;
; Before replacing or removing the app, the installer and the uninstaller close it if
; it is running. electron-builder's default does that with Stop-Process / taskkill,
; which terminates the app and its bot server (resources\backend\tradebot-backend.exe)
; at once, possibly in the middle of a trading cycle. Instead, this first asks the
; running app to quit the normal way: it starts the installed app with --quit, which
; makes the running instance stop the bot server gracefully (a running trading cycle
; finishes first, at most about 3.5 minutes), and waits for it to exit. Only if it is
; still running after that does the default check (with its kill) take over.

!include "LogicLib.nsh"
!include "getProcessInfo.nsh"
Var pid

!define TRADEBOT_QUIT_WAIT_SECONDS 240

!macro customCheckAppRunning
  !insertmacro IS_POWERSHELL_AVAILABLE
  !insertmacro FIND_PROCESS "${APP_EXECUTABLE_FILENAME}" $R0
  ${If} $R0 == 0
  ${AndIf} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
    DetailPrint `Asking "${PRODUCT_NAME}" to stop its trading bot safely...`
    Exec `"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --quit`
    StrCpy $R1 0
    ${Do}
      Sleep 1000
      IntOp $R1 $R1 + 1
      !insertmacro FIND_PROCESS "${APP_EXECUTABLE_FILENAME}" $R0
      ${If} $R0 != 0
        ${Break}
      ${EndIf}
      ${If} $R1 = 10
        DetailPrint `Waiting for "${PRODUCT_NAME}" to finish its current trading cycle...`
      ${EndIf}
    ${LoopWhile} $R1 < ${TRADEBOT_QUIT_WAIT_SECONDS}
  ${EndIf}
  ; Still running (or it could not be asked): electron-builder's default check.
  !insertmacro _CHECK_APP_RUNNING
!macroend
