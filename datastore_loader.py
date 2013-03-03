# Loads CKAN resources into the CKAN Datastore
# to make an API out of static files.

import argparse, urllib2, json, os.path, re, unicodedata, logging

# Command-line arguments

parser = argparse.ArgumentParser(description='Load a CKAN resource into the Datastore.')
parser.add_argument('base_url', type=str, help='The CKAN base URL, e.g. http://www.example.org')
parser.add_argument('api_key', type=str, help='A CKAN API key that can edit the resource.')
parser.add_argument('resource_id', type=str, nargs="?", help='The resource GUID, or omit to load all resources in the CKAN catalog.')
args = parser.parse_args()

# Configure Logging

logging.basicConfig(format="%(message)s")
log = logging.getLogger()
log.setLevel(logging.INFO)

# Utilities

class UserError(Exception):
	def __init__(self, msg):
		super(UserError, self).__init__(msg)
		
class UnhandledError(Exception):
	def __init__(self, msg):
		super(UnhandledError, self).__init__(msg)

def ckan(action, params, error_handler=None):
	# Invoke a CKAN API action.
	
	# Build the request.	
	request = urllib2.Request(
		"%s/api/3/action/%s" % (args.base_url, action),
		json.dumps(params))
	request.add_header("Content-Type", 'application/json')
	request.add_header("Authorization", args.api_key)
	
	# Execute the request.
	try:
		response = urllib2.urlopen(request)
	except urllib2.HTTPError as e:
		# HTTPError is a special exception that can be
		# treated as an HTTP response object. We'll do
		# an error check below.
		response = e
	
	# If the response was OK, parse the JSON and return
	# just the "result" part of the response.	
	if response.getcode() == 200:
		return json.load(response)["result"]
		
	# Call failed. Raise an exception with an informative
	# error message.
	
	response_data = response.read()
	try:
		# Attempt to load the response as JSON.
		msg = json.loads(response_data)
		
		# Allow the caller to prevent the raising of an exception.
		# Pass the response JSON object to the error handler, and
		# if it returns True then we'll silently ignore the error
		# and return None.
		if error_handler:
			if error_handler(msg["error"]):
				return None
				
		# If the response JSON has an "error" key, then use that
		# as the error message. Reformat it back into JSON so we
		# have a string.
		msg = msg["error"]
		msg = json.dumps(msg, sort_keys=True, indent=4) 
	except:
		# If we can't decode the response as JSON, use the raw
		# response as the error message.
		msg = response_data
		
	if response.getcode() == 403:
		# Custom message for 403.
		raise UserError("Permission denied. CKAN indicated the API key was not valid for modifying the resource. (%s)" % msg)
	else:
		# Generic message. We should not show this to the user if
		# we can help it.
		raise UnhandledError("CKAN API call failed: " + msg)

# Main routines.

def upload_resource_to_datastore(resource):
	# Given a resource (passed as a dict that has at least
	# id and url), downloads the resource's raw content,
	# parses it into a table, and uploads the table to
	# the CKAN Datastore.

	log.info("Processing %s (%s)." % (resource["url"], resource["id"]))

	# Load the resource so we know the column headers, datatypes, etc.
	schema, recorditer = parse_resource(resource)
	
	# Upload the resource to the Datastore.
	try:
		upload_resource_records(resource, schema, recorditer)
	except UserError as e:
		# There was some data format error. Instead of raising
		# the error, we should do something so that we are able
		# to pass the inferred schema back to the caller so that
		# the user can edit the schema to try to avoid the error.
		raise
	
def parse_resource(resource):
	# Given a resource (passed as a dict that has at least
	# id and url), returns a tuple of
	#  * the schema used to load the file
	#  * the resource as a table, as given by messytables (an iterator over rows) 
	
	# Start a download of the resource. The actual download
	# will probably commence inside AnyTableSet.from_fileobj
	# when .read() is called.
	payload = urllib2.urlopen(resource["url"])
	
	# Schema defaults. We'll build up the schema with defaults
	# from the actual resource so that it is easy for the data
	# owner to customize the schema later.
	schema = {
	}
	
	# Utility function that's like dict.get() but works on nested
	# dicts and takes a path through to dicts as arguments. Returns
	# None if no value is found.
	#   e.g. schema_get('format', 'name')
	#        This returns schema["format"]["name"], or None if
	#        "format" isn't in schema or "name" isn't in schema["format"].
	def schema_get(*path, **kwargs):
		if len(path) < 1: raise ValueError()
		v = schema
		for p in path: v = v.get(p, {})
		if v == { }: v = kwargs.get("default", None)
		return v
	
	# Utility function that sets a value in a set of nested dicts.
	# Takes a path plus a value as arguments.
	#   e.g. schema_set('format', 'name', 'zip')
	#        This is equivalent to:
	#          schema["format"]["name"] = "zip"
	#        but creating dicts as necessary along the way.
	def schema_set(*path_and_value):
		if len(path_and_value) < 2: raise ValueError()
		path = path_and_value[0:-2]
		field = path_and_value[-2]
		value = path_and_value[-1]
		container = schema
		for p in path:
			container = container.setdefault(p, {})
		container[field] = value
	
	# Parse the payload.
	
	# Get the table data format.
	
	if schema_get('format', 'name') == None:
		# Auto-detect format.
		from messytables import AnyTableSet as data_format
		data_format_args = { }
		
	elif schema_get('format', 'name') in ("csv", "tsv"):
		# "format" = {
		#   "name": "csv" | "tsv",
		#   "delimiter": ",",
		#   "quotechar": "\"",
		#   "encoding": "utf-8"
		# }
		
		# Load as CSV/TSV.
		from messytables import CSVTableSet as data_format
		
		# Default load parameters.
		data_format_args = {
			"delimiter": "," if schema_get('format', 'name') == "csv" else "\t",
			"quotechar": '"',
			"encoding": None,
		}
		
		# Override parameters from the schema.
		for n in ("delimiter", "quotechar", "encoding"):
			v = schema_get("format", n)
			if v:
				data_format_args[n] = v
		
	else:
		raise UserError("Invalid format name in schema. Allowed values are: csv, tsv.")
		
	# If the user specifies a ZIP container, then parse the
	# payload as a ZIP file and pass the format parameters
	# into ZIPTableSet so it knows how to parse the inner files.
	
	if schema_get("container", "name") == "zip":
		# "container = {
		#   "name": "zip"
		# }
		
		from messytables import ZIPTableSet
		table_set = ZIPTableSet.from_fileobj(payload,
			inner_data_format=data_format,
			inner_parser_args=data_format_args)

	elif schema_get("container", "name") != None:
		raise UserError("Invalid container name in schema. Allowed values are: zip.")

	# If format parameters were given explicity, do not use a container.
	# Just parse according to the specified format.

	elif schema_get('format', 'name') != None:
		table_set = data_format.from_fileobj(payload, **data_format_args)
		
	# If no container and no format was specified, auto-detect everything.
	
	else:
		# Get the MIME type and the file extension.
		mime_type = payload.info()["Content-Type"]
		filename, fileext = os.path.splitext(resource["url"])
		if fileext.strip() in ("", "."):
			fileext = None
		else:
			fileext = fileext[1:] # strip off '.'
		
		# Use the AnyTableSet to guess all parsing parameters.
		from messytables import AnyTableSet
		try:
			table_set = AnyTableSet.from_fileobj(payload, mimetype=mime_type, extension=fileext)
		except Exception as e:
			raise UserError("The file format could not be recognized: %s" % str(e))
		
		# Provide the container information that may have been guessed.
		if type(table_set).__name__ == "ZIPTableSet":
			schema_set("container", "name", "zip")
		
	table = table_set.tables[0]

	# Provide the CSV parser settings that may have been guessed.
	if type(table).__name__ == "CSVRowSet":
		schema_set("format", "name", "tsv" if table.delimiter == "\t" else "csv")
		schema_set("format", "delimiter", table.delimiter)
		schema_set("format", "quotechar", table.quotechar)
		schema_set("format", "encoding", table.encoding)
        
	# Get the column header names and the row offset to the data.
	
	# Start by guessing.
	from messytables import headers_guess, headers_processor
	offset, headers = headers_guess(table.sample)
	
	# Overwrite the has_headers and offset values with the schema, if present.
	has_headers = schema_get("header", "present", default=True)
	offset = schema_get("header", "offset", default=offset)
	
	# Set the header information back into the schema.
	schema_set("header", "present", True)
	schema_set("header", "offset", offset)
	
	# Override the header names with what is specified in the schema.
	for cidx in schema_get("columns", default={}):
		try:
			headers[cidx] = schema_get("columns", cidx, "name", default=headers[cidx])
		except IndexError:
			pass # ignore schema problems?
	
	# Since SQL column names are not case sensitive, prevent any
	# uniqueness clashes by converting all to lowercase. While
	# we're at it, also trim spaces.
	headers = [h.lower().strip() for h in headers]
	
	# Ensure the headers are valid SQL-ish column names:
	#  1st character: letter or underscore
	#  subsequent characters: letter, number, or underscore
	for i, header in enumerate(headers):
		# To play nice with international characters, convert to ASCII
		# by replacing extended characters with their closest ASCII
		# equivalent where possible.
		header = u"".join(c for c in unicodedata.normalize('NFKD', header)
			if not unicodedata.combining(c))
		
		# If there is an invalid 1st character, prepend an underscore.
		if not re.match("^[a-z_]", header):
			header = "_" + header
			
		# Replace any invalid characters with "".
		header = re.sub("[^a-z0-9_]", "", header)
		
		# And force to an ASCII byte string, which should be possible by now.
		headers[i] = str(header)

	# TODO: Check that there is not an insane number of columns.
	# That could crash headers_make_unique. 

	# Ensure the headers are unique, and not too long. Postgres
	# supports 63 (64?)-char-long col names, but that's ridiculous.
	from messytables import headers_make_unique
	headers = headers_make_unique(headers, max_length=24)
	
	# Provide the header names to the user in the schema.
	for i in xrange(len(headers)):
		schema_set("columns", i, "name", headers[i])

	# Skip the header row.
	# (Add one to begin with content, not the header.)
	from messytables import offset_processor
	table.register_processor(offset_processor(offset + 1))
	
	# Try to guess the datatypes.
	import messytables.types
	from messytables import type_guess, types_processor
	datatypes = type_guess(
		table.sample,
		[
			messytables.types.StringType,
			messytables.types.IntegerType,
			messytables.types.FloatType,
			messytables.types.DecimalType,
			messytables.types.DateType
		],
		strict=True
		)
	messytable_datastore_type_mapping = {
		messytables.types.StringType: 'text',
		messytables.types.IntegerType: 'numeric',  # 'int' may not be big enough,
						# and type detection may not realize it needs to be big
		messytables.types.FloatType: 'float',
		messytables.types.DecimalType: 'numeric',
		messytables.types.DateType: 'timestamp',
	}
	datatypes = [messytable_datastore_type_mapping[type(t)] for t in datatypes] # convert objects to strings
	
	# Override the datatypes from the schema.
	for cidx in schema_get("columns", default={}):
		try:
			datatypes[cidx] = schema_get("columns", cidx, "type", default=datatypes[cidx])
		except IndexError:
			pass # ignore schema problems?
	
	# Provide the column data type names to the user in the schema.
	for i in xrange(len(headers)):
		schema_set("columns", i, "type", datatypes[i])
		
	# Validate that the datatypes are all legit.
	for dt in datatypes:
		if dt not in ("text", "int", "float", "bool", "numeric", "date", "time", "timestamp", "json"):
			raise UserError("Invalid data type in schema: %s" % dt)
			
	# Validate that every column has information in the schema.
	for i in xrange(max(schema["columns"])+1):
		if i not in schema["columns"]:
			raise UserError("The schema is missing information for column %d." % i)

	return schema, table

def upload_resource_records(resource, schema, recorditer):
	# Given the parsed resource ready to be loaded, now actually
	# pass off the data to the Datastore API.

	# First try to delete any existing datastore for the resource.
	# If the error from CKAN has __type == "Not Found Error",
	# silently continue --- it means there is no datastore for
	# this resource yet.
	ckan("datastore_delete", { "resource_id": resource["id"] },
		error_handler = lambda err : err["__type"] == "Not Found Error")
	
	# Create the datastore.
	ckan("datastore_create", {
		"resource_id": resource["id"],
		"fields": [
			{
				"id": col["name"],
				"type": col["type"],
			} for cidx, col in sorted(schema["columns"].items()) ]
		})
		# TODO: also send primary_key, indexes?
	
	# Helper function to send rows in batches.
	def chunky(iterable, n):
		chunk = []
		for x in iterable:
			chunk.append(x)
			if len(chunk) == n:
				yield chunk
				chunk = []
		if len(chunk) > 0:
			yield chunk			
			
	# Helper function to format the raw string value from the file
	# in a format appropriate for the JSON call to the API, which
	# will pass the value off to PostreSQL. Validates the content.
	def format_value(cellvalue, datatype, rownum, colnum, colname):
		# Return value must be JSON-serializable so we can pass it
		# through the API. Then datastore had better know how to
		# convert that into a string for the SQL statement.
		
		# The empty string is invalid for columns besides text,
		# unless we treat it as a database NULL.
		if cellvalue.strip() == "" and datatype != "text":
			return None # db NULL
		
		# Get the type converter for the column's datatype.
		import messytables.types
		datastore_messytable_type_mapping = {
			'text': messytables.types.StringType,
			'int': messytables.types.IntegerType,
			'float': messytables.types.FloatType,
			'numeric': messytables.types.FloatType, # DecimalType is not JSON serializable
			'timestamp': messytables.types.DateType,
		}
		typ = datastore_messytable_type_mapping[datatype]
		
		# Normalize the raw value.
		try:
			return typ().cast(cellvalue)
		except ValueError:
			# If normalization fails, the user has provided an
			# invalid value.
			raise UserError('The value "%s" in row %d column %d (%s) is invalid for a %s column.' % (cellvalue, rownum+1, colnum+1, colname, datatype))
			
	# Utility function to convert a messytables row to a
	# datastore API row.
	def format_record(rownum, row, columns):
		# Convert the table row that looks like
		#   [ Cell(value=___), ... ]
		# into a dictionary for datastore that looks like:
		#   { col1name: ___, ... }
		if len(columns) != len(row):
			raise UserError("Row %d does not have %d columns." % (rownum, len(columns)))
		row2 = { }
		for i, col in enumerate(columns):
			row2[col["name"]] = format_value(
				row[i].value, col["type"],
				rownum, i, col["name"])
		return row2
			 
	# Convert the schema column info into a list.
	
	columns = []
	for i, col in sorted(schema["columns"].items()):
		columns.append(col)
			
	# Finally, the actual procedure to chunk the rows and do
	# the upload.
	
	rownum = 0
	for rows in chunky(recorditer, 1024):
		log.info("Uploading row %d..." % rownum)
		
		# Re-format messytables row into the list of dicts expected
		# by the CKAN Datastore API. Also track the row number for
		# error reporting.
		payload = []
		for row in rows:
			payload.append(format_record(rownum, row, columns))
			rownum += 1
			
		# Execute API call.
		ckan("datastore_upsert", {
			"resource_id": resource["id"],
			"method": "insert",
			"records": payload,
			})
		
#####################################################################

if args.resource_id == None:
	# Upload all packages.
	packages = ckan("package_list", {})
	for package_id in packages:
		# Get the package's first resource.
		pkg = ckan("package_show", { "id": package_id })
		resource = pkg["resources"][0]
		try:
			upload_resource_to_datastore(resource)
		except UserError as e:
			log.error(e)
else:
	# Upload a particular resource.
	resource = ckan("resource_show", { "id": args.resource_id })
	upload_resource_to_datastore(resource)
	