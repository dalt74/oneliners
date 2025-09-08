if [ -e ~/.autopath ] ; then
    addons=$(cat ~/.autopath | while read item ; do [ -z $item ] && continue ; echo "$PATH" | tr ":" "\n" | grep -q "^$item$" || echo -n :$item ; done)
    PATH=$PATH:$addons
fi
