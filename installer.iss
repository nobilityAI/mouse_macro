[Setup]
AppName=Johns Elbow
AppVersion=1.0
DefaultDirName={autopf}\JohnsElbow
DefaultGroupName=Johns Elbow
OutputBaseFilename=JohnsElbow-Setup
SetupIconFile=.github\build-resources\johns_elbow.ico
Compression=lzma
SolidCompression=yes

[Files]
Source: "dist\JohnsElbow.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Johns Elbow"; Filename: "{app}\JohnsElbow.exe"

