// Debugging tools
;;; function alert_dump(obj, name) {
;;;     var s = name ? name + ":\n" : obj.toString ? obj.toString() + ":\n" : '';
;;;     for (var i in obj) s += i + ': ' + obj[i] + "\n";
;;;     alert(s);
;;; }
function carp() {
    try {
        $('#debug').append($('<p>').text($.makeArray(arguments).join(' ')));
    } catch(e) { }
    try {
        console.log.apply(this, arguments);
    } catch(e) {
        try {
            console.log(arguments);
        } catch(e) { }
    }
}

function arr2map(arr) {
    var rv = {};
    for (var i = 0; i < arr.length; i++) rv[ arr[i] ] = 1;
    return rv;
}

var BASE_PATH = window.BASE_URL ? BASE_URL.replace(/\/$/,'') : '';

function prepend_base_path_to(url) {
    if (url.charAt(0) == '/') url = BASE_PATH + url;
    return url;
}

// localization
var _;
if (window.gettext) _ = gettext;
else {
    carp('i18n broken -- gettext is not defined');
    _ = function(s) { return s; };
}

//// URL management
var URLS = {
/*
    test: function($target) {
        alert($target.attr('id'));
        location.href = 'http://google.com';
    }
*/
};

// mapping for containers to the URLs that give out their base content (what's there when the page loads)
var BASES = {
    content: '/nm/'
};
//// End of URL management

var ContentByHashLib = {};

( function($) { $(document).ready( function() {
    
    // We need to remember what URL is loaded in which element,
    // so we can load or not load content appropriately on hash change.
    var LOADED_URLS = ContentByHashLib.LOADED_URLS = {};
    ContentByHashLib.LOADED_MEDIA = {};
    
    var ORIGINAL_TITLE = document.title;
    
    // If the hash changes before all ajax requests complete,
    // we want to cancel the pending requests. MAX_REQUEST is actually the number
    // of the last hashchange event. Each ajax request then remembers the state
    // of this variable when it was issued and if it's obsolete by the time it's
    // finished, the results are discarded. It's OK to discard it because it
    // never gets into LOADED_URLS.
    var MAX_REQUEST = 0;
    
    // When a sequence of URLs to load into various elements is given,
    // the requests are stored in this fifo and their results are
    // rendered into the document as they become ready, but always in order.
    var LOAD_BUF = [];
    // These are the indices into the LOAD_BUF array -- MIN_LOAD is the index
    // of the request to be processed next (there should never be any defined
    // fields in LOAD_BUF at position less than MIN_LOAD).
    // MAX_LOAD should pretty much always be LOAD_BUF.length - 1.
    var MIN_LOAD, MAX_LOAD = -1;
    
    // When something is loaded into an element that has no base view (in urls.js),
    // and the user hits back, we need to reload. But then we don't want to reload again,
    // so keep information about whether we manipulated the content, so we can
    // abstain from reloading if we have not.
    var PAGE_CHANGED = 0;
    
    var ADDRESS_POSTPROCESS = ContentByHashLib.ADDRESS_POSTPROCESS = {};
    
    function object_empty(o) {
        for (var k in o) return false;
        return true;
    }
    function keys(o) {
        var rv = [];
        for (var k in o) rv.push(k);
        return rv;
    }
    
    // Returns the closest parent that is a container for a dynamically loaded piece of content, along with some information about it.
    function closest_loaded(el) {
        while ( el && (!el.id || !LOADED_URLS[ el.id ]) ) {
            el = el.parentNode;
        }
        if (!el) return null;
        return { container: el, id: el.id, url: LOADED_URLS[ el.id ], toString: function(){return this.id} };
    }
    ContentByHashLib.closest_loaded = closest_loaded;
    
    function inject_content($target, data, address) {
        // whatever was loaded inside, remove it from LOADED_URLS
        if (!object_empty(LOADED_URLS)) {
            var sel = '#'+keys(LOADED_URLS).join(',#');
            $target.find(sel).each(function() {
                delete LOADED_URLS[ this.id ];
            });
        }
        
        $target.removeClass('loading').html(data);
        if ($target.hasClass('noautoshow')) {} else $target.show();
        var newtitle = $('#doc-title').text();
        document.title = (newtitle ? newtitle+' | ' : '') + ORIGINAL_TITLE;
        try { dec_loading(); } catch(e) { }
        if (address != undefined) {
            LOADED_URLS[ $target.attr('id') ] = address;
        }
        PAGE_CHANGED++;
        $target.trigger('content_added');
    }
    
    function inject_error_message(load_id) {
        var info = LOAD_BUF[ load_id ];
        if (!info) {
            carp('bad LOAD_BUF index passed to inject_error_message: '+load_id);
            return;
        }
        var $target = $('#'+info.target_id);
        var response_text = info.xhr.responseText;
        var $err_div = $('<div class="error-code"></div>').append(
            $('<a>reload</a>').css({display:'block'}).click(function(){
                load_content(info);
                return false;
            })
        );
        LOADED_URLS[ info.target_id ] = 'ERROR:'+info.address;
        try {
            $err_div.append( JSON.parse(response_text).message );
        } catch(e) {
            // Render the whole HTML in an <object>
            $obj = $('<object type="text/html" width="'
            + ($target.width() - 6)
            + '" height="'
            + Math.max($target.height(), 300)
            + '">'
            + '</object>');
            
            function append_error_data() {
                $obj.attr({ data:
                    'data:text/html;base64,'
                    + Base64.encode(response_text)
                }).appendTo( $err_div );
            }
            
            if (window.Base64) {
                append_error_data();
            }
            else {
                request_media(MEDIA_URL + 'js/base64.js');
                $(document).one('media_loaded', append_error_data);
            }
        }
        $target.empty().append($err_div);
    }
    
    // Check if the least present request has finished and if so, shift it
    // from the queue and render the results, and then call itself recursively.
    // This effectively renders all finished requests from the first up to the
    // first pending one, where it stops. If all requests are finished,
    // the queue gets cleaned and the indices reset.
    function draw_ready() {
        
        // Slide up to the first defined request or to the end of the queue
        while (!LOAD_BUF[ MIN_LOAD ] && LOAD_BUF.length > MIN_LOAD+1) MIN_LOAD++;
        
        // If the queue is empty, clean it
        if (!LOAD_BUF[ MIN_LOAD ]) {
//            ;;; carp("Emptying buffer");
            LOAD_BUF = [];
            MIN_LOAD = undefined;
            MAX_LOAD = -1;
            return;
        }
        var info = LOAD_BUF[ MIN_LOAD ];
        
        if (!info.data) return; // Not yet ready
        
        delete LOAD_BUF[ MIN_LOAD ];
        while (LOAD_BUF.length > MIN_LOAD+1 && !LOAD_BUF[ ++MIN_LOAD ]) {}
        var $target = $('#'+info.target_id);
        if ($target && $target.jquery && $target.length) {} else {
            carp('Could not find target element: #'+info.target_id);
            try { dec_loading(); } catch(e) { }
            draw_ready();
            return;
        }
        
        inject_content($target, info.data, info.address);
        
        // Check next request
        draw_ready();
    }
    
    // This removes a request from the queue
    function cancel_request( load_id ) {
        var info = LOAD_BUF[ load_id ];
        delete LOAD_BUF[ load_id ];
        $('#'+info.target_id).removeClass('loading');
        try { dec_loading(); } catch(e) { }
        
        carp('Failed to load '+info.address+' into '+info.target_id);
    }
    
    // Take a container and a URL. Give the container the "loading" class,
    // fetch the URL, push the request into the queue, and when it finishes,
    // check for requests ready to be loaded into the document.
    function load_content(arg) {
        var target_id = arg.target_id;
        var address = arg.address;
        ;;; carp('loading '+address+' into #'+target_id);
            
        // An empty address means we should revert to the base state.
        // If one is not set up for the given container, reload the whole page.
        if (address.length == 0) {
            if (BASES[ target_id ]) {
                address = BASES[ target_id ];
            } else {
                if (PAGE_CHANGED) location.reload();
                return;
            }
        }
        
        $('#'+target_id).addClass('loading');
        try { show_loading(); } catch(e) { }
        
        var url = prepend_base_path_to(address);
        url = $('<a>').attr('href', url).get(0).href;
        var load_id = ++MAX_LOAD;
        if (MIN_LOAD == undefined || load_id < MIN_LOAD) MIN_LOAD = load_id;
        LOAD_BUF[ load_id ] = {
            target_id: target_id,
            address: address
        };
        $.ajax({
            url: url,
            type: 'GET',
            success: function(data) {
                if (this.request_no < MAX_REQUEST) {
                    cancel_request( this.load_id );
                }
                else {
                    LOAD_BUF[ this.load_id ].data = data;
                }
                draw_ready();
                if (this.custom_success) try {
                    this.custom_success();
                } catch(e) { carp('Failed success callback (load_content)', e, this); }
            },
            error: function(xhr) {
                LOAD_BUF[ this.load_id ].xhr = xhr;
                inject_error_message( this.load_id );
                cancel_request( this.load_id );
                show_ajax_error(xhr);
                draw_ready();
                if (this.custom_error) try {
                    this.custom_error();
                } catch(e) { carp('Failed error callback (load_content)', e, this); }
            },
            load_id: load_id,
            request_no: MAX_REQUEST,
            custom_success: arg.success_callback,
            custom_error: arg.error_callback,
            original_options: arg
        });
    }
    ContentByHashLib.load_content = load_content;
    
    function reload_content(container_id) {
        var addr = LOADED_URLS[ container_id ] || '';
        load_content({
            target_id: container_id,
            address: addr
        });
    }
    ContentByHashLib.reload_content = reload_content;
    
    function unload_content(container_id, options) {
        if (!options) options = {};
        delete LOADED_URLS[ container_id ];
        var $container = $('#'+container_id);
        if (!options.keep_content) {
            $container.empty();
        }
    }
    ContentByHashLib.unload_content = unload_content;
    
    // We want location.hash to exactly describe what's on the page.
    // #url means that the result of $.get(url) be loaded into the #content div.
    // #id::url means that the result of $.get(url) be loaded into the #id element.
    // Any number of such specifiers can be concatenated, e.g. #/some/page/#header::/my/header/
    // If URLS[ foo ] is set (in urls.js), and #foo is present,
    // then the function is called given the $target as argument
    // and nothing else is done for this specifier.
    function load_by_hash() {
        var hash = location.hash.substr(1);
//        ;;; carp('load #'+MAX_REQUEST+'; hash: '+hash)
        
        // Figure out what should be reloaded and what not by comparing the requested things with the loaded ones.
        var requested = {};
        var specifiers = hash.split('#');
        var ids_map = {};
        var ids_arr = [];
        for (var i = 0; i < specifiers.length; i++) {
            var spec = specifiers[ i ];
            var address = spec;
            var target_id = 'content';
            if (spec.match(/^([-\w]+)::(.*)/)) {
                target_id  = RegExp.$1;
                address = RegExp.$2;
            }
            
            // check if the address is not to be automagically modified
            if (ADDRESS_POSTPROCESS[ address ]) {
                address = ADDRESS_POSTPROCESS[ address ];
                specifiers[i] = (spec.indexOf('::')>=0 ? spec.substr(0, spec.indexOf('::') + '::'.length) : '') + address;
                location.hash = specifiers.join('#');
                return;
            }
            
            requested[ target_id ] = address;
            ids_map[ target_id ] = 1;
            ids_arr.push(target_id);
        }
        for (var k in LOADED_URLS)  if (!ids_map[ k ]) {
            ids_map[ k ] = 1;
            ids_arr.push(k);
        }
        var is_ancestor = {};
        for (var ai = 0; ai < ids_arr.length; ai++) {
            for (var di = 0; di < ids_arr.length; di++) {
                if (ai == di) continue;
                var aid = ids_arr[ai];
                var did = ids_arr[di];
                var $d = $('#'+did);
                if ($d && $d.length) {} else continue;
                var $anc = $d.parent().closest('#'+aid);
                if ($anc && $anc.length) {
                    is_ancestor[ aid+','+did ] = 1;
                }
            }
        }
        var processed = {};
        var reload_target = {};
        while (!object_empty(ids_map)) {
            
            // draw an element that's independent on any other in the list
            var ids = [];
            for (var id in ids_map) ids.push(id);
            var indep;
            for (var i = 0; i < ids.length; i++) {
                var top_el_id = ids[i];
                var is_independent = true;
                for (var j = 0; j < ids.length; j++) {
                    var low_el_id = ids[j];
                    if (low_el_id == top_el_id) continue;
                    if (is_ancestor[ low_el_id + ',' + top_el_id ]) {
                        is_independent = false;
                        break;
                    }
                }
                if (is_independent) {
                    indep = top_el_id;
                    delete ids_map[ top_el_id ];
                    break;
                }
            }
            if (!indep) {
                carp(ids_map);
                throw('Cyclic graph of elements???');
            }
            
            var result = {};
            for (var par in processed) {
                // if we went over an ancestor of this element
                if (is_ancestor[ par+','+indep ]) {
                    // and we marked it for reload
                    if (processed[ par ].to_reload) {
                        // and we're not just recovering
                        if (requested[ indep ]) {
                            // then reload no matter if url changed or not
                            result.to_reload = true;
                            break;
                        }
                        else {
                            // no need to recover when parent gets reloaded
                            result.to_reload = false;
                            break;
                        }
                    }
                }
            }
            
            // If parent didn't force reload or delete,
            if (result.to_reload == undefined) {
                // and the thing is no longer requested and we don't have the base loaded,
                if (!requested[ indep ] && LOADED_URLS[ indep ] != '') {
                    // then reload the base
                    result.to_reload = 1;
                }
            }
            
            if (result.to_reload == undefined) {
                // If the requested url changed,
                if (requested[ indep ] != LOADED_URLS[ indep ]) {
                    // mark for reload
                    result.to_reload = 1;
                }
            }
            
            // If we want to reload but no URL is set, default to the base
            if (result.to_reload && !requested[ indep ]) {
                requested[ indep ] = '';
            }
            
            processed[ indep ] = result;
        }
        // Now we figured out what to reload.
        
        for (var target_id in requested) {
            if (!processed[ target_id ].to_reload) {
                continue;
            }
            var address = requested[ target_id ];
            
            // A specially treated specifier. The callback should set up LOADED_URLS properly.
            // FIXME: Rewrite
            if (URLS[address]) {
                URLS[address](target_id);
                continue;
            }
            
            load_content({
                target_id: target_id,
                address: address
            });
        }
    }
    
    // Fire hashchange event when location.hash changes
    var CURRENT_HASH = '';
    $(document).bind('hashchange', function() {
//        carp('hash: ' + location.hash);
        MAX_REQUEST++;
        $('.loading').removeClass('loading');
        try { hide_loading(); } catch(e) { }
        load_by_hash();
    });
    setTimeout( function() {
        try {
            if (location.hash != CURRENT_HASH) {
                CURRENT_HASH = location.hash;
                $(document).trigger('hashchange');
            }
        } catch(e) { carp(e); }
        setTimeout(arguments.callee, 50);
    }, 50);
    // End of hash-driven content management
    
    // Loads stuff from an URL to an element like load_by_hash but:
    // - Only one specifier (id-url pair) can be given.
    // - URL hash doesn't change.
    // - The specifier is interpreted by adr to get the URL from which to ajax.
    //   This results in support of relative addresses and the target_id::rel_base::address syntax.
    function simple_load(specifier) {
        var target_id;
        var colon_index = specifier.indexOf('::');
        if (colon_index < 0) {
            target_id = 'content';
        }
        else {
            target_id = specifier.substr(0, colon_index);
        }
        
        var address = get_hashadr(specifier);
        
        if (LOADED_URLS[target_id] == address) {
            $('#'+target_id).slideUp('fast');
            unload_content(target_id);
            return;
        }
        
        load_content({target_id:target_id, address:address});
    }
    ContentByHashLib.simple_load = simple_load;
    
    // Set up event handlers
    $('.simpleload,.simpleload-container a').live('click', function(evt) {
        if (evt.button != 0) return true;    // just interested in left button
        simple_load($(this).attr('href'));
        return false;
    });
    $('.hashadr,.hashadr-container a').live('click', function(evt) {
        if (evt.button != 0) return true;    // just interested in left button
        adr($(this).attr('href'));
        return false;
    });
})})(jQuery);


// Manipulate the hash address.
// 
// We use http://admin/#/foo/ instead of http://admin/foo/.
// Therefore, <a href="bar/"> won't lead to http://admin/#/foo/bar/ as we need but to http://admin/bar/.
// To compensate for this, use <a href="javascript:adr('bar/')> instead.
// adr('id::bar/') can be used too.
// 
// adr('bar/#id::baz/') is the same as adr('bar/'); adr('id::baz/').
// Absolute paths and ?var=val strings work too.
// 
// Alternatively, you can use <a href="bar/" class="hashadr">.
// The hashadr class says clicks should be captured and delegated to function adr.
// A third way is to encapsulate a link (<a>) into a .hashadr-container element.
// 
// The target_id::rel_base::address syntax in a specifier means that address is taken as relative
// to the one loaded to rel_base and the result is loaded into target_id.
// For example, suppose that location.hash == '#id1::/foo/'. Then calling
// adr('id2::id1::bar/') would be like doing location.hash = '#id1::/foo/#id2::/foo/bar/'.
// 
// The second argument is an object where these fields are recognized:
// - hash: a custom hash string to be used instead of location.hash,
// - just_get: Instructs the function to merely return the modified address (without the target_id).
//   Using this option disables the support of multiple '#'-separated specifiers.
//   Other than the first one are ignored.
function adr(address, options) {
    if (address == undefined) {
        carp('No address given to adr()');
        return;
    }
    
    // '#' chars in the address separate invividual requests for hash modification.
    // First deal with the first one and then recurse on the subsequent ones.
    if (address.charAt(0) == '#') address = address.substr(1);
    var hashpos = (address+'#').indexOf('#');
    var tail = address.substr(hashpos+1);
    address = address.substr(0, hashpos);
    
    if (!options) options = {};
    var hash = (options.hash == undefined) ? location.hash : options.hash;
    
    // Figure out which specifier is concerned.
    var target_id = '';
    // But wait, if target_id::rel_base::address was specified,
    // then get the modifier address and insert it then as appropriate.
    var new_address, reg_res;
    if (reg_res = address.match(/([-\w]*)::([-\w]*)::(.*)/)) {
        var rel_base;
        target_id = reg_res[1];
        rel_base  = reg_res[2];
        address   = reg_res[3];
        if (rel_base.length) rel_base  += '::';
        new_address = adr(rel_base+address, {hash:hash, just_get:1})
        if (options.just_get) return new_address;
    }
    // OK, go on figuring out which specifier is concerned.
    else if (reg_res = address.match(/([-\w]*)::(.*)/)) {
        target_id = reg_res[1];
        address   = reg_res[2];
    }
    
    // If no hash is present, simply use the address.
    if (hash.length <= 1) {
        var newhash;
        if (target_id.length == 0) {
            newhash = address;
        }
        else {
            newhash = target_id + '::' + address
        }
        if (options.just_get) return newhash;
        else {
            location.hash = newhash;
            return;
        }
    }
    
    // Figure out the span in the current hash where the change applies.
    var start = 0;
    var end;
    var specifier_prefix = '';
    if (target_id.length == 0) {
        for (; start >= 0; start = hash.indexOf('#', start+1)) {
            end = (hash+'#').indexOf('#', start+1);
            if (hash.substring(start, end).indexOf('::') < 0) {
                start++;
                break;
            }
        }
        if (start < 0) {
            hash += '#';
            start = end = hash.length;
        }
    }
    else {
        var idpos = hash.indexOf(target_id+'::');
        if (idpos == -1) {
            hash += '#';
            start = end = hash.length;
            specifier_prefix = target_id + '::';
        }
        else {
            start = idpos + target_id.length + '::'.length;
            end = (hash+'#').indexOf('#', start);
        }
    }
    // Now, hash.substring(start,end) is the address we need to modify.
    
    // Figure out whether we replace the address, append to it, or what.
    // Move start appropriately to denote where the part to replace starts.
    
    var newhash;
    var addr_start = start;
    var old_address = hash.substring(start,end);
    
    // We've not gotten the address from a previous recursive call, thus modify the address as needed.
    if (new_address == undefined) {
        new_address = address;
        
        // empty address -- remove the specifier
        if (address.length == 0) {
            // but in case of just_get, return the original address for the container (relative "")
            if (options.just_get) new_address = hash.substring(start,end);
            start = hash.lastIndexOf('#',start);
            start = Math.max(start,0);
            addr_start = start;
        }
        // absolute address -- replace what's in there.
        else if (address.charAt(0) == '/') {
        }
        // set a get parameter
        else if (address.charAt(0) == '&') {
            var qstart = old_address.indexOf('?');
            if (qstart < 0) qstart = old_address.length;
            var oldq = old_address.substr(qstart);
            var newq = oldq;
            if (oldq.length == 0) {
                newq = '?' + address.substr(1);
            }
            else  {
                var assignments = address.substr(1).split(/&/);
                for (var i = 0; i < assignments.length; i++) {
                    var ass = assignments[i];
                    var vname = (ass.indexOf('=') < 0) ? ass : ass.substr(0, ass.indexOf('='));
                    if (vname.length == 0) {
                        carp('invalid assignment: ' + ass);
                        continue;
                    }
                    var vname_esc = vname.replace(/\W/g, '\\$1');
                    var vname_re = new RegExp('(^|[?&])' + vname_esc + '(?:=[^?&]*)?(&|$)');
                    var changedq = newq.replace(vname_re, '\$1' + ass + '\$2');
                    
                    // vname was not in oldq -- append
                    // the second condition is there so that when we have ?v and call &v we won't get ?v&v but still ?v
                    if (changedq == newq && !vname_re.test(newq)) {
                        newq = newq + '&' + ass;
                    }
                    else {
                        newq = changedq;
                    }
                }
            }
            new_address = old_address.substr(0, qstart) + newq;
        }
        // relative address -- append to the end, but no farther than to a '?'
        else {
            var left_anchor = hash.lastIndexOf('#', start)+1;
            start = (hash.substr(0, end)+'?').indexOf('?', start);
            
            // cut off the directories as appropriate when the address starts with ../
            while (new_address.substr(0,3) == '../' && hash.substring(left_anchor,start-1).indexOf('/') >= 0) {
                new_address = new_address.substr(3);
                start = hash.lastIndexOf('/', start-2)+1;
            }
        }
    }
    
    newhash = hash.substr(0, start) + specifier_prefix + new_address + hash.substr(end);
    
    if (options.just_get) {
        return hash.substring(addr_start, start) + new_address;
    }
    else if (tail) {
        adr(tail, {hash:newhash});
    }
    else {
        location.hash = newhash;
    }
}
// returns address for use in hash, i.e. without BASE_PATH
function get_hashadr(address, options) {
    if (!options) options = {};
    options.just_get = 1;
    return adr(address, options);
}
// returns address for use in requests, i.e. with BASE_PATH prepended
function get_adr(address, options) {
    var hashadr = get_hashadr(address, options);
//    if (hashadr.charAt(0) != '/') hashadr = get_hashadr(hashadr);
    return prepend_base_path_to(hashadr);
        
}


// Dynamic media (CSS, JS) loading
(function() {
    
    // Get an URL to a CSS or JS file, attempt to load it into the document and call callback on success.
    function load_media(url, succ_fn, err_fn) {
        ;;; carp('loading media '+url);
        
        url.match(/(?:.*\/\/[^\/]*)?([^?]+)(?:\?.*)?/);
        $(document).data('loaded_media')[ RegExp.$1 ] = url;
        
        if (url.match(/\.(\w+)(?:$|\?)/))
            var ext = RegExp.$1;
        else throw('Unexpected URL format: '+url);
        
        var abs_url = $('<a>').attr({href:url}).get(0).href;
        
        function stylesheet_present(url) {
            for (var i = 0; i < document.styleSheets.length; i++) {
                if (document.styleSheets[i].href == url) return document.styleSheets[i];
            }
            return false;
        }
        function get_css_rules(stylesheet) {
            try {
                if (stylesheet.cssRules) return stylesheet.cssRules;
                if (stylesheet.rules   ) return stylesheet.rules;
            } catch(e) { carp(e); }
            carp('Could not get rules from: ', stylesheet);
            return;
        }
        
        if (ext == 'css') {
            if (ContentByHashLib.LOADED_MEDIA[ url ] || stylesheet_present(abs_url)) {
                if ($.isFunction(succ_fn)) succ_fn(url);
                return true;
            }
            var tries = 100;
            
            setTimeout(function() {
                if (--tries < 0) {
                    ContentByHashLib.LOADED_MEDIA[ url ] = false;
                    carp('Timed out loading CSS: '+url);
                    if ($.isFunction(err_fn)) err_fn(url);
                    return;
                }
                var ss;
                if (ss = stylesheet_present(abs_url)) {
                    var rules = get_css_rules(ss);
                    if (rules && rules.length) {
                        ContentByHashLib.LOADED_MEDIA[ url ] = true;
                        if ($.isFunction(succ_fn)) succ_fn(url);
                    }
                    else {
                        ContentByHashLib.LOADED_MEDIA[ url ] = false;
                        if (rules) carp('CSS stylesheet empty.');
                        if ($.isFunction(err_fn)) err_fn(url);
                        return;
                    }
                }
                else setTimeout(arguments.callee, 100);
            }, 100);
            
            return $('<link rel="stylesheet" type="text/css" href="'+url+'" />').appendTo($('head'));
        }
        else if (ext == 'js') {
            var $scripts = $('script');
            for (var i = 0; i < $scripts.length; i++) {
                if ($scripts.get(i).src == abs_url) {
                    if ($.isFunction(succ_fn)) succ_fn(url);
                    return true;
                }
            }
            return $.ajax({
                url:       url,
                type:     'GET',
                dataType: 'script',
                success:   function() {
                    ContentByHashLib.LOADED_MEDIA[ this.url ] = true;
                    succ_fn();
                },
                error:     function() {
                    ContentByHashLib.LOADED_MEDIA[ this.url ] = false;
                    err_fn();
                },
                cache:     true
            });
        }
        else throw('Unrecognized media type "'+ext+'" in URL: '+url);
    }
    
    
    var media_queue = [];
    $(document).data('loaded_media', {});
    function init_media() {
        $(document).trigger('media_loaded').data('loaded_media', {});
    }
    function draw_media() {
        if (media_queue.length == 0) {
            init_media();
            return true;
        }
        var url = media_queue.shift();
        load_media(url, draw_media, draw_media);
    }
    
    // Load a CSS / JavaScript file (given an URL) after previously requested ones have been loaded / failed loading.
    function request_media(url) {
        var do_start = media_queue.length == 0;
        media_queue.push(url);
        if (do_start) {
            setTimeout(draw_media,20);
        }
    }
    window.request_media = request_media;
    
})();
