@echo off
REM Purpose: Stage repo changes, create a commit with the current short message, and push the current branch.
REM Usage: checkin.cmd
REM Sample: checkin.cmd
setlocal

set MESSAGE=Update Auto Backup
REM Update MESSAGE before running so the commit summary matches the current change.

for /f %%B in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%B
if /I not "%BRANCH%"=="master" (
  echo checkin.cmd must be run from master. Current branch: %BRANCH%
  exit /b 1
)

git add -A
if errorlevel 1 exit /b 1

git update-index --add --chmod=+x app-stack.sh checkout.sh deploy.sh inspect.sh show-log.sh
if errorlevel 1 exit /b 1

git diff --cached --quiet
if not errorlevel 1 (
  echo No changes to commit.
  exit /b 0
)

git commit -m "%MESSAGE%"
if errorlevel 1 exit /b 1

git push origin master
if errorlevel 1 exit /b 1

endlocal
