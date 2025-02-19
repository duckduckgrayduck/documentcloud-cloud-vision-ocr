"""
This is Add-On allows users to use Google Cloud Vision API to OCR a document. 
"""

import os
import sys
import math
import json
from tempfile import NamedTemporaryFile

# pylint: disable = import-error
from documentcloud.addon import AddOn

# pylint: disable = no-name-in-module
from google.cloud import vision
from google.cloud import storage


class CloudVision(AddOn):
    """OCR your documents using Google Cloud Vision API"""

    # Initialize GCV Variables
    def __init__(self, *args, **kwargs):
        """Initialize GCV Bucket and variables"""
        super().__init__(*args, **kwargs)
        self.setup_credential_file()
        # Set bucket name
        self.bucket_name = "documentcloud_cloudvision_ocr"
        # Instantiate a client for the client libraries 'storage' and 'vision'
        self.storage_client = storage.Client()
        self.vision_client = vision.ImageAnnotatorClient()
        self.bucket = self.storage_client.get_bucket(self.bucket_name)
        # Activate DOCUMENT_TEXT_DETECTION feature
        self.feature = vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)
        # Set file format to PDF
        self.mime_type = "application/pdf"
        # The number of pages that will be grouped in each json response file
        self.batch_size = 1

    def setup_credential_file(self):
        """Sets up Google Cloud credential file"""
        credentials = os.environ["TOKEN"]
        # put the contents into a named temp file
        # and set the var to the name of the file
        with NamedTemporaryFile(delete=False) as gac:
            gac.write(credentials.encode("ascii"))
            gac_name = gac.name
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = gac_name

    def validate(self):
        """Validate that we can run the OCR"""
        if self.get_document_count() is None:
            self.set_message(
                "It looks like no documents were selected. Search for some or "
                "select them and run again."
            )
            return False
        elif not self.org_id:
            self.set_message("No organization to charge.")
            return False
        else:
            num_pages = 0
            for document in self.get_documents():
                num_pages += document.page_count
            try:
                self.charge_credits(num_pages)
            except ValueError:
                return False
        return True

    def json_ocr(self, input_dir, filename):
        """Uploads the PDFs to storage, runs OCR on the documents,
        and collects the gcs location for the repsonses"""
        # Create a remote path.
        # The combination of os.path.basename and os.path.normath
        # extracts the name of the last directory of the path, i.e. 'docs_to_OCR'.
        remote_subdir = os.path.basename(os.path.normpath(input_dir))
        rel_remote_path = os.path.join(remote_subdir, filename)

        # Upload file to Google Cloud Bucket as a blob.
        blob = self.bucket.blob(rel_remote_path)
        blob.upload_from_filename(os.path.join(input_dir, filename))

        # Remote path to the file.
        gcs_source_uri = os.path.join("gs://", self.bucket_name, rel_remote_path)

        # Input source and input configuration.
        gcs_source = vision.GcsSource(uri=gcs_source_uri)
        input_config = vision.InputConfig(
            gcs_source=gcs_source, mime_type=self.mime_type
        )

        # Path to the response JSON files in the Google Cloud Storage.
        # In this case, the JSON files will be saved inside a
        # subfolder of the Cloud version of the input_dir called 'json_output'.
        gcs_destination_uri = os.path.join(
            "gs://", self.bucket_name, remote_subdir, "json_output", filename[:60] + "_"
        )

        # Output destination and output configuration.
        gcs_destination = vision.GcsDestination(uri=gcs_destination_uri)
        output_config = vision.OutputConfig(
            gcs_destination=gcs_destination, batch_size=self.batch_size
        )

        # Instantiate OCR annotation request.
        async_request = vision.AsyncAnnotateFileRequest(
            features=[self.feature],
            input_config=input_config,
            output_config=output_config,
        )

        # The timeout variable tells you when a process takes too long and should be aborted.
        # If the OCR process fails due to timeout, you can try and increase this threshold.
        operation = self.vision_client.async_batch_annotate_files(
            requests=[async_request]
        )
        operation.result(timeout=360)

        return gcs_destination_uri

    def list_blobs(self, gcs_destination_uri):
        """Identifies the responsible blobs and orders them"""
        # Identify the 'prefix' of the response JSON files
        prefix = "/".join(gcs_destination_uri.split("//")[1].split("/")[1:])

        # Use this prefix to extract the correct JSON response
        # files from your bucket and store them as 'blobs' in a list.
        blobs_list = list(self.bucket.list_blobs(prefix=prefix))

        # Order the list by length before sorting it alphabetically,
        # so that the text appears in the correct order in the output file
        blobs_list = sorted(blobs_list, key=lambda blob: len(blob.name))

        return blobs_list

    def set_doc_text(self, document, blobs_list):
        """Uses DC API to set the page text and positions given the OCR resp"""
        pages = []
        for i, blob in enumerate(blobs_list):
            json_string = blob.download_as_string()
            response = json.loads(json_string)
            full_text_response = response["responses"]

            for text_response in full_text_response:
                try:
                    annotation = text_response.get("fullTextAnnotation")
                    if annotation:
                        page = {
                            "page_number": i,
                            "text": annotation["text"],
                            "ocr": "googlecv",
                            "positions": [],  # Initialize positions array
                        }

                        # Extract text position information for words
                        for ann_page in annotation["pages"]:
                            for block in ann_page["blocks"]:
                                for paragraph in block["paragraphs"]:
                                    for word in paragraph["words"]:
                                        normalized_vertices = word["boundingBox"][
                                            "normalizedVertices"
                                        ]
                                        x1 = normalized_vertices[0].get("x", 0)  # Leftmost x-coordinate
                                        x2 = normalized_vertices[1].get("x", 0)  # Rightmost x-coordinate
                                        y1 = normalized_vertices[0].get("y", 0)  # Topmost y-coordinate
                                        y2 = normalized_vertices[2].get("y", 0)  # Bottommost y-coordinate

                                        symbols_list = word["symbols"]
                                        full_text = ''.join(symbol["text"] for symbol in symbols_list)
                                        if 0 <= x1 <= 1 and 0 <= x2 <= 1 and 0 <= y1 <= 1 and 0 <= y2 <= 1:
                                            position_info = {
                                                "text": full_text,
                                                "x1": x1,
                                                "x2": x2,
                                                "y1": y1,
                                                "y2": y2,
                                            }
                                            # Append position information to the page dictionary
                                            page["positions"].append(position_info)

                        pages.append(page)
                    else:
                        page = {
                            "page_number": i,
                            "text": "",
                            "ocr": "googlecv",
                            "positions": [],  # Initialize positions array
                        }
                        pages.append(page)
                except KeyError as e:
                    print(e)
                    self.set_message("Key error- ping us at info@documentcloud.org with the document you're trying to OCR")
                    sys.exit(1)
                except ValueError as v:
                    self.set_message(
                        "Value error - Ping us at info@documentcloud.org"
                        " if you see this more than once."
                    )
                    sys.exit(1)

        # Set the pages with text and position information to the document
        resp = self.client.patch(f"documents/{document.id}/", json={"pages": pages})

    def vision_method(self, document, input_dir, filename):
        """Main method that calls the sub-methods to perform OCR on a doc"""
        # Assign the remote path to the response JSON files to a variable.
        gcs_destination_uri = self.json_ocr(input_dir, filename)
        # Create an ordered list of blobs from these remote JSON files.
        blobs_list = self.list_blobs(gcs_destination_uri)
        self.set_doc_text(document, blobs_list)

    def main(self):
        """For each document, it sends the PDF to Google Cloud Storage and runs OCR"""
        os.mkdir("out")
        if not self.validate():
            # if not validated, return immediately
            return
        for document in self.get_documents():
            pdf_name = f"{document.title}.pdf"
            with open(f"./out/{document.title}.pdf", "wb") as file:
                file.write(document.pdf)
            self.vision_method(document, "out", pdf_name)


if __name__ == "__main__":
    CloudVision().main()
