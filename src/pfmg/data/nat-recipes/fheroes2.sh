#!/bin/sh

export TIMIDITY_CFG=/app/etc/timidity.cfg

HEROES2_RESOURCES_PATH="$HOME/.var/app/io.github.ihhub.Fheroes2/data/fheroes2"
HEROES2_DATA_PATH=$(find "$HEROES2_RESOURCES_PATH" -maxdepth 1 -type d -iname DATA)
HEROES2_AGG_PATH=$(find "$HEROES2_DATA_PATH" -maxdepth 1 -type f -iname HEROES2.AGG 2> /dev/null)
if [[ -n "$HEROES2_AGG_PATH" ]];
then
  # show extra message when demo installed
  HEROES2_OFFER_PATH=$(find "$HEROES2_DATA_PATH" -type f -iname H2OFFER.SMK)
  if [[ -n "$HEROES2_OFFER_PATH" ]];
  then
    if zenity --question --text "Only the demo installed.\nWill be started automatically in 5 sec." --cancel-label "Start demo" --ok-label "Install full version" --timeout 5 ;
    then
      # remove demo files and restart
      rm -rf "$HEROES2_RESOURCES_PATH/DATA"
      fheroes2.sh && exit
    else
      fheroes2
    fi
  else
    fheroes2
  fi
else
  ans=$(zenity --list \
    --text "To play <a href='https://ihhub.github.io/fheroes2/'><b>fheroes2</b></a> you will need assets from the original game or <a href='https://www.gog.com/de/game/heroes_of_might_and_magic_2_gold_edition'>GOG</a>.\nAlternatively, the <a href='https://archive.org/details/HeroesofMightandMagicIITheSuccessionWars_1020'>demo</a> (only one scenario, no campaign and limited assets) can be installed." \
    --title "Complete installation of fheroes2" \
    --column "What to do?   " --column "Requirement   " --column "" \
      'Install GOG version   ' 'EXE installer file   ' '(recommend)' \
      'Manual install   ' 'HoMM2 files   ' '' \
      'Install demo   ' '' ''
  2> /dev/null)

  if [[ $ans == *"GOG"* ]]; then
    file=$(zenity --file-selection --title="Select installer from GOG (*.EXE)")
    # extract files from exe
    innoextract --gog --output-dir $HEROES2_RESOURCES_PATH \
      --include DATA \
      --include GAMES \
      --include MAPS \
      --include MUSIC \
      --include ANIM \
      --include SOUND \
      --include homm2.gog \
      --include homm2.ins \
      $file
    if [[ $? -ne 0 ]]; then
      zenity --error --text "Extraction failed!"
      exit 1
    fi
    # some exe needs to move file from "app" folder
    mv $HEROES2_RESOURCES_PATH/app/* $HEROES2_RESOURCES_PATH/ 2> /dev/null
    rm -rf $HEROES2_RESOURCES_PATH/app/ 2> /dev/null
    # some exe have the "ANIM" in a "HEROES2 subfolder
    mv $HEROES2_RESOURCES_PATH/HEROES2/* $HEROES2_RESOURCES_PATH/ 2> /dev/null
    rm -rf $HEROES2_RESOURCES_PATH/HEROES2/ 2> /dev/null
    
    #extract "ANIM" from bin/cue if exists
    cd $HEROES2_RESOURCES_PATH
    if ls homm2.gog 2> /dev/null ;
    then
      mkdir ANIM
      head -n 3 homm2.ins > img.cue
      bchunk homm2.gog img.cue img > /dev/null
      bsdtar -x -f img01.iso -C ANIM --include "HEROES2/ANIM/*" --strip-components=2
      rm img01.iso img.cue homm2.gog homm2.ins
    fi
    
    fheroes2
  fi
  if [[ $ans == *"demo"* ]]; then
    # just unzip the demo
    wget https://archive.org/download/HeroesofMightandMagicIITheSuccessionWars_1020/h2demo.zip
    unzip -o -q h2demo.zip "DATA/*" "MAPS/*" -d $HEROES2_RESOURCES_PATH
    rm h2demo.zip
    fheroes2
  fi
  if [[ $ans == *"Manual"* ]]; then
    zenity --info \
      --text "You will need to copy the 'ANIM', 'DATA', 'MAPS' and 'MUSIC' folders from Heroes II to the fheroes2 folder.\n\nFor example:\n$HEROES2_RESOURCES_PATH/ANIM\n$HEROES2_RESOURCES_PATH/DATA\n$HEROES2_RESOURCES_PATH/MAPS\n$HEROES2_RESOURCES_PATH/MUSIC" \
      --ok-label "Open folder"
    mkdir -p $HEROES2_RESOURCES_PATH
    
    # open file manager
    dbus-send --session --print-reply --dest=org.freedesktop.FileManager1 \
      --type=method_call /org/freedesktop/FileManager1 org.freedesktop.FileManager1.ShowFolders \
        array:string:"file://$HEROES2_RESOURCES_PATH" string:""
  fi
fi
